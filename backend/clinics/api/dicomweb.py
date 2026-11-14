"""A small, tenant-scoped DICOMweb QIDO-RS/WADO-RS surface for OHIF."""
import json
import uuid

from django.http import HttpResponse, StreamingHttpResponse
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from clinics.access import Capability, imaging_instance_for_user
from clinics.domain import _audit
from clinics.models import ImagingInstance
from clinics.private_storage import PrivateImageStorage
from .permissions import ActiveClinicContext, CapabilityPermission


def _element(vr, value):
    if value is None or value == "":
        return None
    if isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = [value]
    return {"vr": vr, "Value": values}


def _dataset_json(values):
    return {tag: element for tag, element in values.items() if element is not None}


def _study_json(study):
    instances = ImagingInstance.objects.filter(clinic_id=study.clinic_id, series__study=study)
    modalities = sorted({value for value in instances.values_list("series__modality", flat=True) if value})
    return _dataset_json({
        "0020000D": _element("UI", study.study_instance_uid),
        "00080020": _element("DA", instances.order_by("created_at").values_list("metadata__study_date", flat=True).first()),
        "00080061": _element("CS", modalities),
        "00201206": _element("IS", str(study.series.count())),
        "00201208": _element("IS", str(instances.count())),
    })


def _series_json(series):
    return _dataset_json({
        "0020000D": _element("UI", series.study.study_instance_uid),
        "0020000E": _element("UI", series.series_instance_uid),
        "00080060": _element("CS", series.modality),
        "00200011": _element("IS", str(series.series_number) if series.series_number is not None else None),
        "00201209": _element("IS", str(series.instances.filter(clinic_id=series.clinic_id).count())),
    })


def _instance_json(instance):
    meta = instance.metadata or {}
    return _dataset_json({
        "0020000D": _element("UI", instance.series.study.study_instance_uid),
        "0020000E": _element("UI", instance.series.series_instance_uid),
        "00080018": _element("UI", instance.sop_instance_uid),
        "00080016": _element("UI", meta.get("sop_class_uid")),
        "00080060": _element("CS", instance.series.modality),
        "00200013": _element("IS", str(meta.get("instance_number")) if meta.get("instance_number") is not None else None),
    })


class DICOMwebBase(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated, ActiveClinicContext, CapabilityPermission]
    required_capability = Capability.VIEW_IMAGE

    def instances(self):
        queryset = ImagingInstance.objects.filter(clinic_id=self.request.clinic.pk, series__study__kind="dicom")
        membership = self.request.membership
        if membership.has_role("technician") and not membership.has_role("radiologist"):
            queryset = queryset.filter(series__study__examination__assignment__technician_id=self.request.user.pk)
        return queryset.select_related("series__study", "series__study__examination")

    def instance_for_uids(self, study_uid, series_uid, sop_uid):
        instance = self.instances().filter(
            series__study__study_instance_uid=study_uid,
            series__series_instance_uid=series_uid,
            sop_instance_uid=sop_uid,
        ).first()
        if not instance:
            raise NotFound()
        return imaging_instance_for_user(
            user=self.request.user, clinic_id=self.request.clinic.pk, instance_id=instance.pk,
        )

    def open_dataset(self, instance):
        import pydicom

        storage = PrivateImageStorage()
        if not storage.verify(instance.storage_key, checksum_sha256=instance.checksum_sha256, byte_size=instance.byte_size):
            raise NotFound()
        try:
            with storage.open(instance.storage_key) as handle:
                return pydicom.dcmread(handle, stop_before_pixels=True, force=False)
        except (OSError, ValueError):
            raise NotFound()


class DICOMStudyListView(DICOMwebBase):
    def get(self, request):
        studies = {}
        for instance in self.instances():
            study = instance.series.study
            studies[study.pk] = study
        uid_filter = request.query_params.get("StudyInstanceUID") or request.query_params.get("StudyInstanceUIDs")
        uid_filter = set(uid_filter.split(",")) if uid_filter else None
        try:
            offset = max(0, int(request.query_params.get("offset", 0)))
            limit = min(100, max(1, int(request.query_params.get("limit", 100))))
        except ValueError as exc:
            raise ValidationError("DICOMweb offset and limit must be integers.") from exc
        matched = [study for study in studies.values() if not uid_filter or study.study_instance_uid in uid_filter]
        matched = matched[offset:offset + limit]
        for study in matched:
            _audit(request.clinic.pk, request.user, "imaging.study_metadata_accessed", study)
        result = [_study_json(study) for study in matched]
        return HttpResponse(json.dumps(result), content_type="application/dicom+json", headers={"Cache-Control": "private, no-store"})


class DICOMSeriesListView(DICOMwebBase):
    def get(self, request, study_uid):
        instances = self.instances().filter(series__study__study_instance_uid=study_uid)
        if not instances.exists():
            raise NotFound()
        series = {instance.series_id: instance.series for instance in instances}
        for row in series.values():
            _audit(request.clinic.pk, request.user, "imaging.series_metadata_accessed", row)
        return HttpResponse(json.dumps([_series_json(row) for row in series.values()]), content_type="application/dicom+json", headers={"Cache-Control": "private, no-store"})


class DICOMInstanceListView(DICOMwebBase):
    def get(self, request, study_uid, series_uid):
        instances = self.instances().filter(series__study__study_instance_uid=study_uid, series__series_instance_uid=series_uid)
        if not instances.exists():
            raise NotFound()
        for instance in instances:
            _audit(request.clinic.pk, request.user, "imaging.instance_metadata_accessed", instance)
        return HttpResponse(json.dumps([_instance_json(row) for row in instances]), content_type="application/dicom+json", headers={"Cache-Control": "private, no-store"})


class DICOMMetadataView(DICOMwebBase):
    def get(self, request, study_uid, series_uid, sop_uid):
        instance = self.instance_for_uids(study_uid, series_uid, sop_uid)
        ds = self.open_dataset(instance)
        meta = instance.metadata or {}
        tags = {
            "00080016": _element("UI", meta.get("sop_class_uid")),
            "00080018": _element("UI", instance.sop_instance_uid),
            "00080020": _element("DA", meta.get("study_date")),
            "00080060": _element("CS", instance.series.modality),
            "0020000D": _element("UI", study_uid),
            "0020000E": _element("UI", series_uid),
            "00200011": _element("IS", str(instance.series.series_number) if instance.series.series_number is not None else None),
            "00200013": _element("IS", str(meta.get("instance_number")) if meta.get("instance_number") is not None else None),
            "00280002": _element("US", meta.get("samples_per_pixel")),
            "00280004": _element("CS", meta.get("photometric_interpretation")),
            "00280008": _element("IS", meta.get("number_of_frames")),
            "00280010": _element("US", meta.get("rows")),
            "00280011": _element("US", meta.get("columns")),
            "00280100": _element("US", meta.get("bits_allocated")),
            "00280101": _element("US", meta.get("bits_stored")),
            "00280102": _element("US", meta.get("high_bit")),
            "00280103": _element("US", meta.get("pixel_representation")),
            "00020010": _element("UI", str(ds.file_meta.TransferSyntaxUID)),
        }
        return HttpResponse(json.dumps([_dataset_json(tags)]), content_type="application/dicom+json", headers={"Cache-Control": "private, no-store"})


class DICOMInstanceRetrieveView(DICOMwebBase):
    def get(self, request, study_uid, series_uid, sop_uid):
        instance = self.instance_for_uids(study_uid, series_uid, sop_uid)
        storage = PrivateImageStorage()
        if not storage.verify(instance.storage_key, checksum_sha256=instance.checksum_sha256, byte_size=instance.byte_size):
            raise NotFound()
        boundary = "dicom-" + uuid.uuid4().hex
        filename = sop_uid

        def body():
            yield f"--{boundary}\r\nContent-Type: application/dicom\r\nContent-Location: {filename}\r\n\r\n".encode()
            with storage.open(instance.storage_key) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    yield chunk
            yield f"\r\n--{boundary}--\r\n".encode()

        return StreamingHttpResponse(body(), content_type=f'multipart/related; type="application/dicom"; boundary="{boundary}"', headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


class DICOMFramesView(DICOMwebBase):
    def get(self, request, study_uid, series_uid, sop_uid, frame_list):
        import numpy as np
        import pydicom

        instance = self.instance_for_uids(study_uid, series_uid, sop_uid)
        storage = PrivateImageStorage()
        if not storage.verify(instance.storage_key, checksum_sha256=instance.checksum_sha256, byte_size=instance.byte_size):
            raise NotFound()
        with storage.open(instance.storage_key) as handle:
            dataset = pydicom.dcmread(handle, force=False)
        try:
            # Let pydicom select an installed decoder plugin. A blanket
            # is_compressed check rejected valid JPEG/RLE files even when
            # Pillow or pydicom could decode them. Unsupported syntaxes still
            # fail closed below with 406 and never expose storage paths.
            pixel_array = dataset.pixel_array
        except Exception:
            return HttpResponse("This transfer syntax cannot be decoded by the configured pydicom handlers.", status=406, content_type="text/plain")
        try:
            frames = [int(value) - 1 for value in frame_list.split(",")]
        except ValueError as exc:
            raise ValidationError("Frame numbers must be comma-separated positive integers.") from exc
        frame_count = int(getattr(dataset, "NumberOfFrames", 1))
        if not frames or any(frame < 0 or frame >= frame_count for frame in frames):
            raise NotFound()
        boundary = "frame-" + uuid.uuid4().hex
        byte_order = "<" if dataset.file_meta.TransferSyntaxUID.is_little_endian else ">"

        def body():
            for frame_no in frames:
                if frame_count == 1:
                    array = pixel_array
                else:
                    array = pixel_array[frame_no]
                array = np.asarray(array).astype(array.dtype.newbyteorder(byte_order), copy=False)
                yield f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
                yield array.tobytes(order="C")
                yield b"\r\n"
            yield f"--{boundary}--\r\n".encode()

        return StreamingHttpResponse(body(), content_type=f'multipart/related; type="application/octet-stream"; transfer-syntax="1.2.840.10008.1.2.1"; boundary="{boundary}"', headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
