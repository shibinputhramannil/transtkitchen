from decimal import Decimal

from rest_framework import serializers

from apps.inventory.models import MAX_QUANTITY, QUANTITY_DIGITS, QUANTITY_PLACES

from .models import PrepPlan

BUFFER = {"max_digits": 5, "decimal_places": 2, "min_value": Decimal("0"), "max_value": Decimal("200")}


class CreatePlanSerializer(serializers.Serializer):
    plan_date = serializers.DateField()
    buffer_percent = serializers.DecimalField(**BUFFER, required=False, default=Decimal("10"))


class LineChangeSerializer(serializers.Serializer):
    menu_item = serializers.UUIDField()
    planned = serializers.DecimalField(
        max_digits=QUANTITY_DIGITS,
        decimal_places=QUANTITY_PLACES,
        min_value=Decimal("0"),
        max_value=MAX_QUANTITY,
        required=False,
    )
    note = serializers.CharField(max_length=255, required=False, allow_blank=True)


class UpdatePlanSerializer(serializers.Serializer):
    version = serializers.IntegerField(min_value=1)
    buffer_percent = serializers.DecimalField(**BUFFER, required=False)
    lines = LineChangeSerializer(many=True, required=False, default=list)

    def validate_lines(self, lines):
        ids = [line["menu_item"] for line in lines]
        if len(set(ids)) != len(ids):
            raise serializers.ValidationError("Each menu item may appear only once.")
        return lines


class VersionSerializer(serializers.Serializer):
    version = serializers.IntegerField(min_value=1)


class PlanSummarySerializer(serializers.ModelSerializer):
    created_by = serializers.SlugRelatedField(slug_field="email", read_only=True)
    finalized_by = serializers.SlugRelatedField(slug_field="email", read_only=True)

    class Meta:
        model = PrepPlan
        fields = [
            "id",
            "plan_date",
            "status",
            "buffer_percent",
            "version",
            "created_by",
            "created_at",
            "updated_at",
            "finalized_by",
            "finalized_at",
        ]
        read_only_fields = fields
