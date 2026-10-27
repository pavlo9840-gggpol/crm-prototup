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
