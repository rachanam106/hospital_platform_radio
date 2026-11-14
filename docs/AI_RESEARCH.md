# Phase 7 revised research prototype

## Implemented behavior

### Clinician-reviewed symptom history

The existing patient profile now exposes a clinical-history panel only to the existing `radiologist` role, because this codebase has no separate doctor role. The backend re-enforces `PATIENT_CLINICAL_HISTORY` on every operation. The clinician enters original free text, and a small local lexicon/rule extractor returns supported symptom mentions with character offsets, normalized terms, simple present/absent/uncertain assertion, and nearby duration text. Confidence is `null`; the rules have no calibrated confidence.

Extraction creates a pending `ClinicalHistoryEntry`. It never updates patient demographics or replaces earlier history. The clinician can correct or add candidate mentions, confirm them, or reject the extraction. Original text, raw extraction, confirmed corrections, actor, review time and extractor provenance are stored separately. Entries are append-only after review; a later correction requires a new entry. Extraction and review create clinic audit events without putting the source text into audit details.

The extractor is a limited deterministic vocabulary, not a pretrained biomedical NLP model. It may miss synonyms, complex negation scope, experiencer, temporal relationships and language outside its hand-coded patterns. A confirmed structured entry represents clinician confirmation of the entered data, not model confidence or a diagnosis.

### Radiology text workflow

The existing report assistant accepts chest X-ray radiologist-entered text observations, organizes them into the Findings section, preserves indication and technique as separate fields, and exposes claim-attribution checks. CT remains on the manual report workflow; no CT generalization is claimed. The assistant does not process JPG/PNG/DICOM images. It deliberately leaves Impression blank; only the assigned radiologist can write/confirm an impression in the existing draft. Accepting an assistance record does not modify a report; the user copies it into editable fields and uses the ordinary concurrency-protected draft save. Signing, release and immutable versions remain unchanged.

The structurer is the deterministic `rule-based-evidence-structurer`, not a generative model. The verifier uses token similarity and polarity rules to distinguish close attribution, unsupported, contradicted and uncertain claims. It is not NLI/clinical entailment, similarity is not a probability, and it has no clinical significance model. Its checks operate only on provided observation text. No output is a diagnosis.

Current backend routes (all retain session/CSRF, active clinic, assigned radiologist and capability enforcement):

- `GET/POST /api/v1/patients/{patient_id}/clinical-history/entries/` lists existing history entries or submits source text for extraction.
- `POST /api/v1/patients/{patient_id}/clinical-history/entries/{entry_id}/review/` confirms corrected mentions or rejects the pending extraction.
- `POST /api/v1/reports/{report_id}/ai-assist/`, `GET /api/v1/reports/{report_id}/ai-assists/`, and `POST /api/v1/reports/{report_id}/review-ai-assist/` are the existing text-evidence report assistance routes.

The current user-facing frontend reuses the Patient profile dialog for history review and the existing ReportingWorkspace for radiology observations. Manual history and report entry remain available if the optional rule assistant is not useful.

## Dataset and model access gate

No research dataset was downloaded or accessed; no model checkpoint was downloaded or loaded. The reviewed sources and decisions are recorded in [research/datasets/manifest.json](../research/datasets/manifest.json).

| Candidate | What the source documents | Decision |
|---|---|---|
| [Lindvall Lab symptom-extraction-demo](https://github.com/lindvalllab/symptom-extraction-demo) and its underlying [Fareez simulated interview corpus](https://doi.org/10.1038/s41597-022-01423-1) | Demo labels are binary or multi-label symptoms, not clearly token spans or the requested attributes. Underlying paper describes 272 simulated OSCE conversations/transcripts and CC BY 4.0 paper terms; the repository code is GPL-2.0, but exact terms for its redistributed data files need confirmation. | Not downloaded. Potential simulation source only after the Figshare/data-file terms and exact labels are verified. |
| [jfan1256/symptom-extraction](https://github.com/jfan1256/symptom-extraction) | Its README describes hand-annotated symptom token labels derived from n2c2 records. | Not used. The repository MIT code license does not grant n2c2 record access; obtain and verify the separate data-use permission first. |
| [IU X-Ray / Open-i](https://openi.nlm.nih.gov/) | Open-i FAQ lists 7,470 images and 3,955 radiology reports; reports have indication, findings and impression. Those are authored report sections, not independent raw observation annotations. Open-i says image reuse depends on the license displayed per item and requests no redistribution outside the research group. | Not downloaded. Check item/report terms and a research split before use. Any experiment would evaluate report text structuring, not raw-observation-to-report quality. |
| [StanfordAIMI/SRR-T5-SciFive](https://huggingface.co/StanfordAIMI/SRR-T5-SciFive) | The model card describes structuring an already-written report (its example input already has Findings and Impression). It lists 0.2B parameters and an approximately 892 MB F32 checkpoint. A model-weight license was not visible in the inspected card metadata. | Not downloaded or adopted: task mismatch for raw radiologist observations, license needs clarification, and no permitted evaluation set is ready. |

The detected local GPU is a GTX 1650 with 4 GiB VRAM. No model or dataset was run; no CPU/RAM/storage benchmark was performed. No identifiable text is sent to any external service.

## Experiments and results

No pretrained-vs-baseline symptom extraction metrics or text-to-text report-generation metrics are reported. There is no split, patient-level leakage control, annotated held-out set, training, model inference, ROUGE, factual consistency, or radiologist evaluation for this revised task. The previous Phase 7 synthetic claim fixture/results in `research/datasets/synthetic_claims.json` and `research/results/synthetic_verification.json` are a legacy behavior demonstration only; they do not evaluate symptom extraction or report generation and are not evidence for this revised research objective.

The rule extractor and the existing synthetic report-assist fixture have focused regression tests, but tests are not research evaluation. Do not interpret these as clinical accuracy, F1, generalization, hallucination rate or human benefit.

## Reproducible next experiment, pending access

1. Confirm the exact Fareez/Figshare transcript terms and Lindvall CSV version/labels. Use transcripts only if reuse rights and label provenance are clear; preserve source-group IDs and split by original simulated conversation, not transcript fragments.
2. Obtain a symptom span/attribute evaluation set. The current demo labels alone cannot score duration/negation/uncertainty span F1. If no appropriately licensed annotated resource is available, limit the study to explicitly labeled class prediction or do not report model metrics.
3. For report structuring, establish IU report-text reuse terms and define a text-only task. The original report Findings/Impression are target text, not independent raw observation ground truth. Hold out report/patient groups and do not include indication/history in finding evidence.
4. Verify the selected model checkpoint’s license and task fit before download. Keep a rule baseline, fixed split, seed, preprocessing, checkpoint hash and configuration; evaluate supported/unsupported/contradicted/uncertain claims only against human-labeled claims.
5. Run X-ray report text separately. Do not claim CT generalization; no CT experiment is part of this revision.

## Limitations

This is a safe, human-reviewed workflow prototype, not a completed AIML research evaluation. Symptom extraction is lexicon/rules only; report structuring is deterministic; no pretrained model was loaded or trained; no dataset access was verified for this environment. The UI/backend have no separate `doctor` membership role, so history operations use the existing radiologist clinical-history capability. Imaging upload/viewing was not changed. Do not use the extraction or report assistant for diagnosis or autonomous clinical decisions. No clinical validation or production-readiness claim is made.
