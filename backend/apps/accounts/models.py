from django.contrib.auth.models import AbstractUser


class User(AbstractUser):
    """
    Project user model, identical to Django's default for now.

    It exists from the first migration so fields can be added later without
    swapping AUTH_USER_MODEL on a database that already holds users.
    """
