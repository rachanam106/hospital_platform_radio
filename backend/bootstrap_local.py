"""Create a synthetic local clinic owned by an existing Django superuser."""
import os

from django.contrib.auth import get_user_model
from django.db import transaction

from clinics.models import AuditEvent, Clinic, Membership, MembershipRole


bootstrap_username = os.environ.get("BOOTSTRAP_USERNAME", "").strip()
if not bootstrap_username:
    raise RuntimeError("Set BOOTSTRAP_USERNAME to the local Django superuser to bootstrap.")
clinic_name = os.environ.get("BOOTSTRAP_CLINIC_NAME", "Synthetic Demo Imaging Clinic").strip()
if not clinic_name:
    raise RuntimeError("BOOTSTRAP_CLINIC_NAME cannot be blank.")

with transaction.atomic():
    user = get_user_model().objects.get(username=bootstrap_username)
    assert user.is_active and user.is_superuser
    assert Clinic.objects.count() == 0, "A clinic already exists. Stopping."

    clinic = Clinic.objects.create(
        name=clinic_name,
        timezone="Asia/Kolkata",
        default_currency="INR",
        invoice_prefix="DEMO",
    )
    membership = Membership.objects.create(clinic=clinic, user=user)
    role = MembershipRole(
        clinic=clinic,
        membership=membership,
        role=MembershipRole.Role.OWNER,
    )
    role._domain_service_write = True
    role.save()

    event = AuditEvent(
        clinic=clinic,
        actor=user,
        action="clinic.initial_owner_bootstrapped",
        object_type="Membership",
        object_id=str(membership.pk),
        details={"role": "owner", "method": "local_development_bootstrap"},
    )
    event._domain_service_write = True
    event.save()

    print("Clinic:", clinic.name)
    print("Membership:", membership.pk)
    print("Roles:", membership.roles())
    print("Audit event:", event.action)
