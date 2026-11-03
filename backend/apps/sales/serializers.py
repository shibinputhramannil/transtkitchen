from rest_framework import serializers

from .models import SaleLine, SalesImport
from .services import ACKNOWLEDGEABLE


class SalesImportSerializer(serializers.ModelSerializer):
    created_by = serializers.SlugRelatedField(slug_field="email", read_only=True)
    applied_by = serializers.SlugRelatedField(slug_field="email", read_only=True)

    class Meta:
        model = SalesImport
        fields = [
            "id",
            "source",
            "file_name",
            "status",
            "row_count",
            "first_date",
            "last_date",
            "created_by",
            "created_at",
            "applied_by",
            "applied_at",
        ]
        read_only_fields = fields


class SaleLineSerializer(serializers.ModelSerializer):
    menu_item = serializers.SerializerMethodField()

    class Meta:
        model = SaleLine
        fields = [
            "row_number",
            "business_date",
            "sold_at",
            "item_name",
            "external_id",
            "quantity",
            "menu_item",
            "ignored",
            "error",
        ]
        read_only_fields = fields

    def get_menu_item(self, line):
        if line.menu_item is None:
            return None
        return {"id": str(line.menu_item.pk), "name": line.menu_item.name}


class MappingSerializer(serializers.Serializer):
    """Map every row with this item name to a menu item, or ignore those rows."""

    item_name = serializers.CharField(max_length=200)
    menu_item = serializers.UUIDField(required=False, allow_null=True)
    ignore = serializers.BooleanField(default=False)

    def validate(self, attrs):
        if attrs["ignore"] == bool(attrs.get("menu_item")):
            raise serializers.ValidationError("Give either a menu_item or ignore: true.")
        return attrs


class ApplySerializer(serializers.Serializer):
    acknowledged = serializers.ListField(
        child=serializers.ChoiceField(choices=ACKNOWLEDGEABLE), required=False, default=list
    )
