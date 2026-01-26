from django.contrib import admin
from .models import *

@admin.register(Investigator)
class InvestigatorAdmin(admin.ModelAdmin):
    list_display = ("short_code", "full_name", "phone", "is_active")
    def save_model(self, request, obj, form, change):
        obj.role = Person.Role.INVESTIGATOR
        super().save_model(request, obj, form, change)


@admin.register(Technician)
class TechnicianAdmin(admin.ModelAdmin):
    list_display = ("short_code", "full_name", "phone", "is_active")
    def save_model(self, request, obj, form, change):
        obj.role = Person.Role.TECHNICIAN
        super().save_model(request, obj, form, change)


admin.site.register(SenderModel)
admin.site.register(BatteryType)
admin.site.register(GpsSender)
admin.site.register(VehicleMakeModel)
admin.site.register(CheckedVehicle)
admin.site.register(CheckedVehiclePhoto)
admin.site.register(ToolMaterial)
admin.site.register(CheckedVehicleRequirement)
admin.site.register(Operation)
admin.site.register(OperationVehicle)
admin.site.register(WorkAction)
