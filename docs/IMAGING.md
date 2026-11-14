# Imaging management (Phase 3)

## What works

- Authenticated multipart upload accepts JPEG (`.jpg`, `.jpeg`), PNG (`.png`), and DICOM Part 10 files (`.dcm`, `.dicom`, or no suffix when the content is valid).
- Pillow verifies raster content and rejects extension/content mismatches and decompression bombs. pydicom parses DICOM headers using established DICOM handling; Pixel Data is required. DICOM Study/Series/SOP UIDs are validated, embedded Patient ID must match the selected clinic patient's MRN, and the file's SOP UID must match the dataset UID.
- DICOM instances with the same study UID are grouped under one `ImagingStudy`; matching series UIDs are grouped under `ImagingSeries`. The DICOM modality must match the examination modality, and a study UID already associated with a different examination is rejected. Multiple raster files for one examination share a raster study/series and are never represented as DICOM.
- Original bytes are retained. SHA-256, byte size, opaque storage key, media type, original filename, uploader, and upload timestamp are retained with the relational clinic → examination → study → series → instance associations.
- Upload permission is limited to technicians and radiologists with `upload_image`, the actor must be the examination's assigned acquisition operator, and upload is only allowed during `in_progress`. Acquisition cannot complete until at least one registered image file exists in private storage.
- Authenticated content retrieval is clinic-scoped, checks capabilities and assignments, verifies size and SHA-256, uses `private, no-store`, and writes an access audit event. Receptionists cannot access imaging metadata or bytes. A technician without assignment cannot access the examination's images.
- The React imaging workspace uploads multiple files with progress, displays studies/series/instances and safe metadata, previews raster files, and supports basic wheel zoom, pointer pan, and previous/next instance navigation.
- A limited QIDO-RS/WADO-RS surface is available below `/api/v1/dicomweb/` for OHIF: study/series/instance queries, metadata, multipart instance retrieval, and decoded frames for transfer syntaxes supported by the installed pydicom handlers.

## Storage and limits

Local development uses `PRIVATE_IMAGE_STORAGE_ROOT`, defaulting to `backend/private_media/`. It is outside `STATIC_ROOT`, is not served by Django static/media URLs, and is excluded from version control. Keys are random `images/<uuid>` values; source filenames and browser paths are never used as storage paths. The `PrivateImageStorage` interface isolates file staging, promotion, opening, verification, and deletion so an S3-compatible backend can replace it later. No S3 implementation or dependency is included.

Defaults are 100 MiB per file, 500 MiB per request batch, and 512 MiB maximum estimated decoded DICOM pixels. Set `IMAGE_UPLOAD_MAX_BYTES`, `IMAGE_UPLOAD_BATCH_MAX_BYTES`, and `DICOM_MAX_DECODED_BYTES` in environment configuration. Configure the reverse proxy/web server request-body limit to at least the intended batch size. Upload parsing is synchronous; there is no resumable/chunked upload or background transcoding. Normal failures remove staged and promoted files. Hard process termination may leave unreferenced private files; run `python manage.py cleanup_private_image_staging` on a schedule to remove staging/orphan files older than 24 hours.

## DICOMweb and OHIF

OHIF's DICOMweb datasource can use the QIDO-RS and WADO-RS roots independently; see [OHIF's DICOMweb datasource documentation](https://docs.ohif.org/configuration/datasources/dicom-web/) and [Docker deployment documentation](https://docs.ohif.org/deployment/docker/). [`OHIF_CONFIG.example.js`](OHIF_CONFIG.example.js) is a read-only datasource configuration for those endpoints. Set `VITE_OHIF_VIEWER_URL=/ohif/` and `OHIF_DEV_SERVER=http://127.0.0.1:3000` in `frontend/.env`; Vite proxies `/ohif` to the separate OHIF development server and `/api` to Django. Configure the OHIF build's public path and router base as `/ohif/` and `/ohif`, respectively, so its static assets and routes stay under the Vite proxy. At deployment, serve the SPA and Django API through one same-origin reverse proxy. Do not point the browser directly at the container's port 3000: a separate origin needs a deliberate credentialed CORS design and is not the supported session-auth setup. OHIF STOW uploads are disabled; the clinic upload API remains the only ingestion route. 

**Viewer verification status (this workspace): incomplete.** The frontend has an iframe integration point and the DICOMweb adapter implements a limited QIDO/WADO subset, but OHIF itself is not included or reproducibly launched by this repository. The local Docker Desktop Linux engine is unavailable in this environment, so the pinned official OHIF image could not be started. No browser verification was possible; therefore study/series/instance retrieval through OHIF, rendered frames, slice/series navigation, window/level, zoom, and pan must not be treated as verified here. This is a real environment/deployment gap, not a functional viewer placeholder. Before enabling it for users, build/run an OHIF 3 release with `PUBLIC_URL=/ohif/` and the supplied config, behind the same-origin proxy, then verify the browser workflows against this adapter. OHIF supports these viewer tools, but compatibility with this limited adapter remains unproven.

### Transfer syntax and decoder compatibility

The frame endpoint delegates decoding to `pydicom.Dataset.pixel_array` with the packages currently installed by `backend/requirements.txt` in the verified environment: pydicom 3.0.2 and Pillow 11.3.0 (plus NumPy 2.2.6). The installed decoder registry reports:

- Native uncompressed Implicit VR Little Endian, Explicit VR Little Endian, and Explicit VR Big Endian: built-in pydicom support.
- RLE Lossless (`1.2.840.10008.1.2.5`): pydicom plugin available.
- JPEG Baseline 8-bit (`1.2.840.10008.1.2.4.50`) and JPEG Extended 12-bit (`...4.51`): Pillow plugin available.
- JPEG 2000 Lossless (`...4.90`) and JPEG 2000 (`...4.91`): Pillow plugin available.
- JPEG Lossless Process 14 (`...4.70`), JPEG-LS Lossless/Near-lossless (`...4.80`, `...4.81`), and HTJ2K (`...4.201`–`...4.203`): no decoder plugin available in this environment; frame retrieval returns HTTP 406.

This is a tested decoder-plugin inventory for these package versions, not a guarantee for every pixel layout or DICOM object. Installing a different pydicom optional codec changes the inventory and must be reviewed and tested explicitly. The API returns 406 if pixel decoding fails. It does not transcode the original file. This is not universal DICOM compatibility.

The backend tests create synthetic Part 10 instances at runtime (no real patient data): one study with two series and three uncompressed instances validates grouping and retrieval. A JPEG Baseline instance validates a supported compressed decode; a second fixture declares JPEG-LS but carries the JPEG payload, so it exercises the 406 decode-failure path, not genuine JPEG-LS interoperability. The installed decoder registry independently confirms no JPEG-LS handler. The tests validate the authenticated examination association and cross-clinic UID rejection. These files are ephemeral test inputs, not production data or a packaged imaging study.

### Local OHIF launch and same-origin routing

The existing Vite proxy expects a separate OHIF dev server on port 3000. For repeatable setup, use the official pinned OHIF release `ohif/app:v3.9.2` (or build OHIF source at the same release) and supply [`OHIF_CONFIG.example.js`](OHIF_CONFIG.example.js) as its `app-config.js`. The official prebuilt Docker image is intended to serve at the root path; it cannot be mounted under `/ohif/` with correct asset routing merely by changing this project’s Vite rewrite. Build the viewer with public path `/ohif/` and router basename `/ohif` before proxying it through Vite. The exact official Docker image and config-mount mechanisms are documented by OHIF at the links above.

This workspace could not execute that deployment because Docker Desktop's `dockerDesktopLinuxEngine` named pipe was absent even after attempting to start Docker Desktop. As a result, no image was pulled and no OHIF browser actions or browser network requests were observed. The existing Vite proxy/config sample is therefore a development integration contract, not a verified turnkey deployment. Do not configure the iframe against a separate-origin OHIF URL unless you separately design and validate credentialed CORS and cookie handling. This avoids suggesting that the current session-based authorization is preserved across arbitrary origins.

## Security boundaries

- Clinic context is resolved from the validated server-side session; path IDs and DICOM UIDs are then resolved through the active clinic queryset.
- DICOM Patient ID must match the patient's clinic MRN. Patient name, birth date, accession number, and free-text descriptions are not copied into the API metadata. The preserved original file may still contain PHI and is available only to roles with image-view capability (and assignment filtering for technician-only memberships).
- Raster upload has no embedded patient identity, so association comes only from the authenticated examination URL and server-side examination→patient relationship.
- Private storage permissions, encryption at rest, backups, retention/deletion policy, malware scanning, reverse-proxy request limits, and production object-storage IAM are deployment responsibilities. The prototype is not production-ready and must not receive real patient data.

## API routes

- `POST /api/v1/examinations/{id}/imaging/` — multipart form with one or more `files` fields; returns instance metadata and duplicate flags.
- `GET /api/v1/imaging-studies/?examination={id}`, `GET /api/v1/imaging-series/?study={id}`, and `GET /api/v1/imaging-instances/?series={id}` — tenant/role-scoped metadata.
- `GET /api/v1/imaging-instances/{id}/content/` — authorized original file; raster is inline-capable, DICOM is attachment.
- QIDO-RS: `GET /api/v1/dicomweb/studies`, `.../studies/{studyUID}/series`, and `.../studies/{studyUID}/series/{seriesUID}/instances`.
- WADO-RS subset: `GET .../instances/{sopUID}/metadata`, `GET .../instances/{sopUID}`, and `GET .../instances/{sopUID}/frames/{frameNumbers}`.

All APIs require session authentication, active clinic context and `view_image` for read operations, or `upload_image` plus assigned acquisition actor and in-progress state for upload. Reads and ingestion are audited. DICOMweb endpoint conformance is a limited OHIF integration surface, not a certified DICOMweb conformance statement.
