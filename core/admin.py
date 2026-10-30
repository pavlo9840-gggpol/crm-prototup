from django.contrib import admin
from . import models as m


@admin.register(m.Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "sku", "barcode", "price", "current_stock", "is_active")
    search_fields = ("name", "sku", "barcode")
    readonly_fields = ("current_stock",)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(m.Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("number", "created_at", "cashier", "total", "status")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(m.StockMovement)
class MovementAdmin(admin.ModelAdmin):
    list_display = ("created_at", "product", "movement_type", "quantity_change", "balance_after")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


for model in (m.Category, m.Supplier, m.Customer, m.Shift):
    admin.site.register(model)


@admin.register(m.PrintTemplate)
class PrintTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "doc_type", "is_builtin", "is_default", "updated_at")
    readonly_fields = ("layout",)


@admin.register(m.SupplierItem)
class SupplierItemAdmin(admin.ModelAdmin):
    list_display = ("brand", "sku", "name", "supplier", "cost", "qty_avail", "available")
    search_fields = ("sku", "sku_norm", "name", "brand")
    list_filter = ("supplier", "available")


admin.site.register(m.MarkupTier)
admin.site.register(m.CrossRef)


@admin.register(m.CatalogSource)
class CatalogSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "license_name", "version", "commercial_use")
    search_fields = ("name", "license_name", "version")
    readonly_fields = ("created_at",)


@admin.register(m.CatalogImportRun)
class CatalogImportRunAdmin(admin.ModelAdmin):
    list_display = ("source", "started_at", "finished_at", "status", "created_count", "updated_count", "skipped_count", "error_count")
    readonly_fields = tuple(field.name for field in m.CatalogImportRun._meta.fields)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(m.VehicleMake)
class VehicleMakeAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "slug", "source")
    search_fields = ("name", "slug", "name_norm")
    list_filter = ("kind", "source")


@admin.register(m.VehicleModel)
class VehicleModelAdmin(admin.ModelAdmin):
    list_display = ("name", "make", "slug")
    search_fields = ("name", "slug", "make__name")
    list_filter = ("make__kind",)


@admin.register(m.VehicleVariant)
class VehicleVariantAdmin(admin.ModelAdmin):
    list_display = ("label", "model", "year_from", "year_to", "engine_code", "source")
    search_fields = ("label", "engine_code", "model__name", "model__make__name")


@admin.register(m.CatalogPart)
class CatalogPartAdmin(admin.ModelAdmin):
    list_display = ("name", "category_name", "updated_at")
    search_fields = ("name", "category_name", "description")


@admin.register(m.CatalogPartNumber)
class CatalogPartNumberAdmin(admin.ModelAdmin):
    list_display = ("brand", "sku", "part", "verified", "source", "product")
    search_fields = ("brand", "sku", "sku_norm", "part__name")
    list_filter = ("verified", "source")


@admin.register(m.PartFitment)
class PartFitmentAdmin(admin.ModelAdmin):
    list_display = ("part_number", "vehicle_model", "year_from", "year_to", "engine_code", "verified", "source")
    search_fields = ("part_number__sku", "part_number__brand", "vehicle_model__name", "vehicle_model__make__name")
    list_filter = ("verified", "source", "vehicle_model__make__kind")
