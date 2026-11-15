
import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from clinics.domain import _audit
from clinics.models import (
    AccountSecurityState,
    Clinic,
    Membership,
    MembershipRole,
)


class Command(BaseCommand):
    help = "Create the first clinic owner in a fresh production database."

    def handle(self, *args, **options):
        User = get_user_model()

        with transaction.atomic():
            clinics = Clinic.objects.count()

            # Allow safe restarts after successful bootstrap, even after
            # temporary bootstrap credentials have been removed.
            if clinics > 0:
                owner_membership = Membership.objects.filter(
                    clinic=clinic,
                    is_active=True,
                    role_grants__role=MembershipRole.Role.OWNER,
                    role_grants__is_active=True,
                    user__is_superuser=True,
                    user__is_active=True,
                ).select_related("user").distinct().first()

                if owner_membership:
                    self.stdout.write(
                        self.style.SUCCESS(
                            "Initial owner setup is already complete."
                        )
                    )
                    return

                raise CommandError(
                    "A clinic exists, but its initial owner could not be verified. "
                    "Refusing to modify the database."
                )

            if clinics > 0 or User.objects.exists():
                raise CommandError(
                    "Database is not empty. Refusing initial bootstrap."
                )

            username = os.environ.get("INITIAL_OWNER_USERNAME", "").strip()
            password = os.environ.get("INITIAL_OWNER_PASSWORD", "")
            email = os.environ.get("INITIAL_OWNER_EMAIL", "").strip()
            first_name = os.environ.get("INITIAL_OWNER_FIRST_NAME", "").strip()
            last_name = os.environ.get("INITIAL_OWNER_LAST_NAME", "").strip()
            clinic_name = os.environ.get("INITIAL_CLINIC_NAME", "").strip()

            required = {
                "INITIAL_OWNER_USERNAME": username,
                "INITIAL_OWNER_PASSWORD": password,
                "INITIAL_OWNER_EMAIL": email,
                "INITIAL_CLINIC_NAME": clinic_name,
            }

            missing = [
                key for key, value in required.items() if not value
            ]

            if missing:
                raise CommandError(
                    "Missing required bootstrap environment variables: "
                    + ", ".join(missing)
                )

            user = User(
                username=username,
                email=email,
                first_name=first_name,
                last_name=last_name,
            )

            validate_password(password, user=user)

            user = User.objects.create_superuser(
                username=username,
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
            )

            AccountSecurityState.objects.create(
                user=user,
                must_change_password=True,
            )

            clinic = Clinic.objects.create(
                name=clinic_name,
                timezone="Asia/Kolkata",
                default_currency="INR",
                invoice_prefix="RAD",
            )

            membership = Membership(
                clinic=clinic,
                user=user,
            )
            membership.save()

            owner_role = MembershipRole(
                clinic=clinic,
                membership=membership,
                role=MembershipRole.Role.OWNER,
            )
            owner_role._domain_service_write = True
            owner_role.save()

            _audit(
                clinic.id,
                user,
                "clinic.initial_owner_bootstrapped",
                membership,
                {
                    "role": "owner",
                    "method": "one_time_production_bootstrap",
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                "Initial clinic owner created successfully."
            )
        )
        self.stdout.write(f"Clinic: {clinic_name}")
        self.stdout.write(f"Owner username: {username}")
