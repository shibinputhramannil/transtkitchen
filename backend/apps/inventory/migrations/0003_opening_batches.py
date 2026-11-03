"""
Puts each ingredient's existing stock into one opening batch with no expiry
date, so quantity_on_hand equals the sum of its batches from the start of
batch tracking. Existing movements are left exactly as they were.
"""

from django.db import migrations
from django.utils import timezone


def create_opening_batches(apps, schema_editor):
    Ingredient = apps.get_model("inventory", "Ingredient")
    StockBatch = apps.get_model("inventory", "StockBatch")
    now = timezone.now()
    StockBatch.objects.bulk_create(
        StockBatch(
            ingredient=ingredient,
            source="opening",
            quantity_received=ingredient.quantity_on_hand,
            quantity_remaining=ingredient.quantity_on_hand,
            received_at=now,
        )
        for ingredient in Ingredient.objects.filter(quantity_on_hand__gt=0)
    )


def delete_opening_batches(apps, schema_editor):
    # Possible only while no later movement has drawn on them.
    apps.get_model("inventory", "StockBatch").objects.filter(source="opening").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("inventory", "0002_stock_batches"),
    ]

    operations = [
        migrations.RunPython(create_opening_batches, delete_opening_batches),
    ]
