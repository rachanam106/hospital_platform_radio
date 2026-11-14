"""Validated image ingestion and safe DICOM metadata extraction."""
from dataclasses import dataclass
from pathlib import Path

import pydicom
import re
import warnings
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombWarning
from pydicom.errors import InvalidDicomError

from .access import Capability
from .domain import _audit, _authorize, _require_assigned_operator, register_imaging_instance
from .models import Examination, ImagingInstance, ImagingStudy
from .private_storage import PrivateImageStorage

RASTER_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png"}
DICOM_EXTENSIONS = {".dcm", ".dicom", ""}


@dataclass
class ParsedImage:
    kind: str
    media_type: str
    modality: str
    study_uid: str = ""
    series_uid: str = ""
    sop_uid: str = ""
    series_number: int | None = None
    metadata: dict | None = None


def _valid_uid(value, label):
    text = str(value or "")
    if not text or len(text) > 64 or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*", text):
        raise ValidationError(f"DICOM {label} UID is missing or invalid.")
    return text


def parse_image(path, original_name, *, expected_mrn):
    suffix = Path(original_name or "").suffix.lower()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = image.format
                width, height = image.size
                if image_format not in RASTER_FORMATS:
                    raise ValidationError("Only JPEG and PNG raster files are accepted.")
                if suffix and suffix not in ({"JPEG": {".jpg", ".jpeg"}, "PNG": {".png"}}[image_format]):
                    raise ValidationError("File extension does not match its image content.")
                image.verify()
                return ParsedImage(
                    kind=ImagingStudy.Kind.RASTER,
                    media_type=RASTER_FORMATS[image_format],
                    modality="RASTER",
                    metadata={"width": width, "height": height, "format": image_format},
                )
    except UnidentifiedImageError:
        pass
    except OSError as exc:
        raise ValidationError("Raster image content is invalid.") from exc

    if suffix not in DICOM_EXTENSIONS:
        raise ValidationError("Unsupported file content. Upload JPEG, PNG, or a DICOM Part 10 file.")
    try:
        dataset = pydicom.dcmread(str(path), defer_size=1, force=False)
    except (InvalidDicomError, OSError, ValueError) as exc:
        raise ValidationError("DICOM file content is invalid or incomplete.") from exc
    patient_id = str(getattr(dataset, "PatientID", "")).strip()
    if not patient_id or patient_id.casefold() != expected_mrn.strip().casefold():
        raise ValidationError("DICOM Patient ID does not match the selected clinic patient record.")
    study_uid = _valid_uid(getattr(dataset, "StudyInstanceUID", ""), "Study Instance")
    series_uid = _valid_uid(getattr(dataset, "SeriesInstanceUID", ""), "Series Instance")
    sop_uid = _valid_uid(getattr(dataset, "SOPInstanceUID", ""), "SOP Instance")
    file_sop_uid = str(getattr(getattr(dataset, "file_meta", None), "MediaStorageSOPInstanceUID", ""))
    if file_sop_uid and file_sop_uid != sop_uid:
        raise ValidationError("DICOM file identity is inconsistent.")
    transfer_syntax = getattr(getattr(dataset, "file_meta", None), "TransferSyntaxUID", None)
    if not transfer_syntax:
        raise ValidationError("DICOM file is missing its transfer syntax declaration.")
    if not getattr(dataset, "SOPClassUID", None):
        raise ValidationError("DICOM image is missing its SOP Class UID.")
    pixel_element = next((dataset.get_item(tag, keep_deferred=True) for tag in (0x7FE00010, 0x7FE00008, 0x7FE00009) if dataset.get_item(tag, keep_deferred=True) is not None), None)
    if pixel_element is None or pixel_element.length <= 0:
        raise ValidationError("DICOM object does not contain image pixel data.")
    try:
        rows, columns = int(dataset.Rows), int(dataset.Columns)
        frames = int(getattr(dataset, "NumberOfFrames", 1))
        samples = int(getattr(dataset, "SamplesPerPixel", 1))
        bits = int(getattr(dataset, "BitsAllocated", 16))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValidationError("DICOM image dimensions are missing or invalid.") from exc
    if min(rows, columns, frames, samples, bits) <= 0 or rows * columns * frames * samples * bits // 8 > settings.DICOM_MAX_DECODED_BYTES:
        raise ValidationError("DICOM image dimensions exceed the configured decoded image limit.")
    modality = str(getattr(dataset, "Modality", "OT")).upper()[:32]
    try:
        series_number = int(dataset.SeriesNumber) if getattr(dataset, "SeriesNumber", None) is not None else None
    except (TypeError, ValueError):
        series_number = None
    # Deliberately omit PatientName, PatientID, birth date, accession number and free text.
    metadata = {
        "sop_class_uid": str(getattr(dataset, "SOPClassUID", "")),
        "study_date": str(getattr(dataset, "StudyDate", ""))[:8],
        "instance_number": int(dataset.InstanceNumber) if str(getattr(dataset, "InstanceNumber", "")).isdigit() else None,
        "rows": int(dataset.Rows) if str(getattr(dataset, "Rows", "")).isdigit() else None,
        "columns": int(dataset.Columns) if str(getattr(dataset, "Columns", "")).isdigit() else None,
        "samples_per_pixel": int(dataset.SamplesPerPixel) if str(getattr(dataset, "SamplesPerPixel", "")).isdigit() else 1,
        "bits_allocated": int(dataset.BitsAllocated) if str(getattr(dataset, "BitsAllocated", "")).isdigit() else None,
        "bits_stored": int(dataset.BitsStored) if str(getattr(dataset, "BitsStored", "")).isdigit() else None,
        "high_bit": int(dataset.HighBit) if str(getattr(dataset, "HighBit", "")).isdigit() else None,
        "pixel_representation": int(dataset.PixelRepresentation) if str(getattr(dataset, "PixelRepresentation", "")).isdigit() else 0,
        "photometric_interpretation": str(getattr(dataset, "PhotometricInterpretation", ""))[:32],
        "number_of_frames": int(dataset.NumberOfFrames) if str(getattr(dataset, "NumberOfFrames", "")).isdigit() else 1,
        "transfer_syntax_uid": str(transfer_syntax),
    }
    return ParsedImage(
        kind=ImagingStudy.Kind.DICOM,
        media_type="application/dicom",
        modality=modality,
        study_uid=study_uid,
        series_uid=series_uid,
        sop_uid=sop_uid,
        series_number=series_number,
        metadata=metadata,
    )


def _upload_audit(*, clinic_id, actor, instance, action, details=None):
    _audit(clinic_id, actor, action, instance, details or {})


@transaction.atomic
def ingest_uploads(*, clinic_id, actor, membership, examination_id, files, storage=None):
    storage = storage or PrivateImageStorage()
    _authorize(membership, actor, clinic_id, Capability.UPLOAD_IMAGE)
    examination = Examination.objects.select_for_update().get(pk=examination_id, clinic_id=clinic_id)
    if examination.status != Examination.Status.IN_PROGRESS:
        raise ValidationError("Images may be uploaded only while acquisition is in progress.")
    _require_assigned_operator(examination, actor)
    if not files:
        raise ValidationError("Select at least one image file.")

    staged, parsed = [], []
    total_size = 0
    stored_keys = []
    try:
        for uploaded in files:
            item = storage.stage(uploaded)
            staged.append((uploaded, item))
            total_size += item.byte_size
            if total_size > settings.IMAGE_UPLOAD_BATCH_MAX_BYTES:
                raise ValidationError(f"A single upload request must be at most {settings.IMAGE_UPLOAD_BATCH_MAX_BYTES} bytes in total.")
            parsed.append(parse_image(item.path, uploaded.name, expected_mrn=examination.patient.medical_record_number))

        results = []
        for (uploaded, item), image in zip(staged, parsed):
            duplicate = ImagingInstance.objects.filter(
                clinic_id=clinic_id, series__study__examination=examination,
                checksum_sha256=item.checksum_sha256,
            ).select_related("series__study").first()
            if duplicate:
                if duplicate.sop_instance_uid != image.sop_uid and image.kind == ImagingStudy.Kind.DICOM:
                    raise ValidationError("A duplicate file checksum conflicts with an existing DICOM instance identity.")
                _upload_audit(clinic_id=clinic_id, actor=actor, instance=duplicate, action="imaging.duplicate_upload", details={"checksum_sha256": item.checksum_sha256})
                results.append({"instance": duplicate, "duplicate": True})
                continue

            if image.kind == ImagingStudy.Kind.DICOM:
                if examination.modality.strip().upper() != image.modality:
                    raise ValidationError("DICOM modality does not match the examination modality.")
                existing_study = ImagingStudy.objects.filter(clinic_id=clinic_id, study_instance_uid=image.study_uid).first()
                if existing_study and existing_study.examination_id != examination.pk:
                    raise ValidationError("DICOM Study Instance UID is already associated with a different examination.")
                study = ImagingStudy.objects.filter(clinic_id=clinic_id, examination=examination, study_instance_uid=image.study_uid).first()
                series = study.series.filter(clinic_id=clinic_id, series_instance_uid=image.series_uid).first() if study else None
                study_id = study.pk if study else None
                series_id = series.pk if series else None
                study_uid, series_uid, sop_uid = image.study_uid, image.series_uid, image.sop_uid
                metadata = image.metadata
            else:
                study = ImagingStudy.objects.filter(clinic_id=clinic_id, examination=examination, kind=ImagingStudy.Kind.RASTER).first()
                series = study.series.filter(clinic_id=clinic_id).first() if study else None
                study_id = study.pk if study else None
                series_id = series.pk if series else None
                study_uid = series_uid = sop_uid = ""
                metadata = image.metadata or {}

            key = storage.promote(item)
            stored_keys.append(key)
            instance = register_imaging_instance(
                clinic_id=clinic_id, actor=actor, membership=membership,
                examination_id=examination.pk, kind=image.kind, modality=image.modality,
                storage_key=key, media_type=image.media_type, original_name=Path(uploaded.name).name[:255],
                byte_size=item.byte_size, checksum_sha256=item.checksum_sha256,
                study_id=study_id, series_id=series_id, study_instance_uid=study_uid,
                series_instance_uid=series_uid, series_number=image.series_number,
                sop_instance_uid=sop_uid,
                metadata=metadata,
            )
            results.append({"instance": instance, "duplicate": False})
        _audit(clinic_id, actor, "imaging.upload_batch_completed", examination, {"file_count": len(results), "new_file_count": sum(not x["duplicate"] for x in results)})
        return results
    except Exception:
        for key in stored_keys:
            storage.delete(key)
        raise
    finally:
        for _, item in staged:
            item.cleanup()
