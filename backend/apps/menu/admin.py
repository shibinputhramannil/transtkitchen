from django.contrib import admin

from .models import MenuItem, MenuItemAlias, RecipeLine


class RecipeLineInline(admin.TabularInline):
    model = RecipeLine
    extra = 0
    autocomplete_fields = ["ingredient"]


@admin.register(MenuItem)
class MenuItemAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "is_active"]
    list_filter = ["is_active"]
    list_select_related = ["organization"]
    search_fields = ["name", "organization__name"]
    autocomplete_fields = ["organization"]
    inlines = [RecipeLineInline]


@admin.register(MenuItemAlias)
class MenuItemAliasAdmin(admin.ModelAdmin):
    list_display = ["external_name", "external_id", "menu_item", "source"]
    list_select_related = ["menu_item"]
    search_fields = ["external_name", "external_id", "menu_item__name"]
    autocomplete_fields = ["organization", "menu_item"]
