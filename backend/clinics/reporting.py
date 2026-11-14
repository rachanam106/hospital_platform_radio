"""Modality-specific report structures; all clinical text is entered by a radiologist."""

REPORT_TEMPLATES = {
    "xray": {
        "key": "xray", "label": "X-ray", "modalities": ("CR", "DX", "DR", "XR", "X-RAY"),
        "fields": ("technique", "findings", "impression", "additional_comments"),
        "sections": ("Technique", "Findings", "Impression", "Additional comments"),
    },
    "ct": {
        "key": "ct", "label": "CT", "modalities": ("CT",),
        "fields": ("technique", "comparison", "findings", "impression", "additional_comments"),
        "sections": ("Technique", "Comparison", "Findings", "Impression", "Additional comments"),
    },
    "mri": {
        "key": "mri", "label": "MRI", "modalities": ("MR", "MRI"),
        "fields": ("technique", "sequences", "comparison", "findings", "impression", "additional_comments"),
        "sections": ("Technique", "Sequences", "Comparison", "Findings", "Impression", "Additional comments"),
    },
    "ultrasound": {
        "key": "ultrasound", "label": "Ultrasound", "modalities": ("US", "ULTRASOUND"),
        "fields": ("technique", "findings", "impression", "additional_comments"),
        "sections": ("Technique", "Findings", "Impression", "Additional comments"),
    },
    "general": {
        "key": "general", "label": "General radiology", "modalities": (),
        "fields": ("technique", "comparison", "findings", "impression", "additional_comments"),
        "sections": ("Technique", "Comparison", "Findings", "Impression", "Additional comments"),
    },
}


def template_for_modality(modality):
    normalized = (modality or "").strip().upper()
    return next((key for key, item in REPORT_TEMPLATES.items() if normalized in item["modalities"]), "general")


def templates_for_modality(modality):
    selected = template_for_modality(modality)
    return [REPORT_TEMPLATES[selected], REPORT_TEMPLATES["general"]] if selected != "general" else [REPORT_TEMPLATES["general"]]
