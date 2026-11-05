"""
Creates the first Django admin account from environment variables, for hosts
without a shell (such as Render's free plan). It runs at every startup and is
safe to repeat:

- Without DJANGO_SUPERUSER_EMAIL and DJANGO_SUPERUSER_PASSWORD it does nothing.
- If a user with that email already exists, it changes nothing about them.
- Otherwise it creates a superuser, checking the email address and the
  password against the same rules as registration.

The password is never printed. Remove both variables once the account exists.
"""

import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction

EMAIL_VARIABLE = "DJANGO_SUPERUSER_EMAIL"
PASSWORD_VARIABLE = "DJANGO_SUPERUSER_PASSWORD"


class Command(BaseCommand):
    help = "Create the first admin account from environment variables, once."

    def handle(self, *args, **options):
        email = os.environ.get(EMAIL_VARIABLE, "").strip()
        password = os.environ.get(PASSWORD_VARIABLE, "")
        if not email and not password:
            return
        if not email or not password:
            raise CommandError(f"Set both {EMAIL_VARIABLE} and {PASSWORD_VARIABLE}, or neither.")
        try:
            # Saving a model does not validate its EmailField, so check it here.
            validate_email(email)
        except ValidationError:
            raise CommandError(f"{EMAIL_VARIABLE} is not a valid email address.") from None

        User = get_user_model()
        if User.objects.filter(email__iexact=email).exists():
            self.stdout.write(
                f"Admin bootstrap: an account for {email} already exists and was left unchanged. "
                f"Remove {EMAIL_VARIABLE} and {PASSWORD_VARIABLE}."
            )
            return

        candidate = User(email=User.objects.normalize_email(email))
        try:
            validate_password(password, candidate)
        except ValidationError as error:
            # The messages describe the rules, never the password itself.
            raise CommandError(
                f"{PASSWORD_VARIABLE} does not meet the password rules: {' '.join(error.messages)}"
            ) from None

        try:
            with transaction.atomic():
                User.objects.create_superuser(email=email, password=password)
        except IntegrityError:
            # Another instance created it at the same moment.
            self.stdout.write(f"Admin bootstrap: an account for {email} already exists and was left unchanged.")
            return
        self.stdout.write(
            f"Admin bootstrap: created the admin account {email}. "
            f"Remove {EMAIL_VARIABLE} and {PASSWORD_VARIABLE} now."
        )
