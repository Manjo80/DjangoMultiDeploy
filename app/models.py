# -*- coding: utf-8 -*-
from django.db import models
from django.core.exceptions import ValidationError


class Person(models.Model):
    class Role(models.TextChoices):
        INVESTIGATOR = "INV", "Ermittler"
        TECHNICIAN = "TECH", "Techniker"

    short_code = models.CharField("Kuerzel", max_length=20, unique=True)
    full_name = models.CharField("Name", max_length=120)
    phone = models.CharField("Telefon", max_length=40, blank=True, default="")
    role = models.CharField("Rolle", max_length=10, choices=Role.choices)
    is_active = models.BooleanField("Aktiv", default=True)

    class Meta:
        ordering = ["short_code"]

    def __str__(self):
        return f"{self.short_code} - {self.full_name}"


# Proxy-Models (getrennte Admin-Listen)
class Investigator(Person):
    class Meta:
        proxy = True
        verbose_name = "Ermittler"
        verbose_name_plural = "Ermittler"


class Technician(Person):
    class Meta:
        proxy = True
        verbose_name = "Techniker"
        verbose_name_plural = "Techniker"


class SenderModel(models.Model):
    name = models.CharField("Modell", max_length=80, unique=True)
    manufacturer = models.CharField("Hersteller", max_length=80, blank=True, default="")
    has_fixed_battery = models.BooleanField("Fest verbauter Akku", default=False)
    supports_external_power = models.BooleanField("Feststrom moeglich", default=False)
    profile_capable = models.BooleanField("Profil-faehig (A9/Gecko)", default=False)
    notes = models.TextField("Notizen", blank=True, default="")

    def __str__(self):
        return self.name


class BatteryType(models.Model):
    name = models.CharField("Akkutyp", max_length=80, unique=True)
    capacity_mah = models.PositiveIntegerField("Kapazitaet (mAh)")
    voltage_v = models.DecimalField("Spannung (V)", max_digits=4, decimal_places=2, null=True, blank=True)
    notes = models.TextField("Notizen", blank=True, default="")

    def __str__(self):
        return f"{self.name} ({self.capacity_mah} mAh)"


class GpsSender(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Verfuegbar"
        IN_USE = "IN_USE", "Im Einsatz"
        DEFECT = "DEFECT", "Defekt"
        LOST = "LOST", "Verloren"

    class StorageLocation(models.TextChoices):
        STATION = "STATION", "Dienststelle"
        GATO2 = "GATO2", "GATO2"
        STAC1 = "STAC1", "STAC1"

    asset_tag = models.CharField(max_length=50, unique=True, blank=True, default="")
    serial_number = models.CharField(max_length=80, unique=True, blank=True, default="")
    model = models.ForeignKey(SenderModel, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.AVAILABLE)
    storage_location = models.CharField(max_length=20, choices=StorageLocation.choices, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    def clean(self):
        if not self.asset_tag and not self.serial_number:
            raise ValidationError("Inventarnummer oder Seriennummer erforderlich.")
        if self.status == self.Status.AVAILABLE and not self.storage_location:
            raise ValidationError("Lagerort erforderlich bei 'verfuegbar'.")


class VehicleMakeModel(models.Model):
    make = models.CharField(max_length=80)
    model = models.CharField(max_length=80)

    def __str__(self):
        return f"{self.make} {self.model}"


class CheckedVehicle(models.Model):
    make_model = models.ForeignKey(VehicleMakeModel, on_delete=models.PROTECT)
    type_name = models.CharField(max_length=120, blank=True, default="")
    build_year = models.CharField(max_length=20, blank=True, default="")
    description = models.TextField(blank=True, default="")
    is_workshop_safe = models.BooleanField(default=False)


class CheckedVehiclePhoto(models.Model):
    checked_vehicle = models.ForeignKey(CheckedVehicle, on_delete=models.CASCADE, related_name="photos")
    image = models.ImageField(upload_to="checked_vehicles/")
    caption = models.CharField(max_length=200, blank=True, default="")


class ToolMaterial(models.Model):
    name = models.CharField(max_length=80, unique=True)

    def __str__(self):
        return self.name


class CheckedVehicleRequirement(models.Model):
    checked_vehicle = models.ForeignKey(CheckedVehicle, on_delete=models.CASCADE)
    tool_material = models.ForeignKey(ToolMaterial, on_delete=models.PROTECT)


class Operation(models.Model):
    name = models.CharField(max_length=120)
    operation_id = models.CharField(max_length=60, blank=True, default="")
    investigator = models.ForeignKey(
        Person, on_delete=models.PROTECT,
        limit_choices_to={"role": Person.Role.INVESTIGATOR}
    )
    investigator_phone = models.CharField(max_length=40, blank=True, default="")


class OperationVehicle(models.Model):
    operation = models.ForeignKey(Operation, on_delete=models.CASCADE)
    user_name = models.CharField(max_length=120)
    owner_name = models.CharField(max_length=120, blank=True, default="")
    owner_address = models.TextField(blank=True, default="")
    user_address = models.TextField(blank=True, default="")
    make = models.CharField(max_length=80)
    model = models.CharField(max_length=80)
    plate = models.CharField(max_length=30, blank=True, default="")
    build_year = models.CharField(max_length=20, blank=True, default="")
    color = models.CharField(max_length=40, blank=True, default="")
    checked_vehicle = models.ForeignKey(CheckedVehicle, on_delete=models.SET_NULL, null=True, blank=True)


class WorkAction(models.Model):
    class ActionType(models.TextChoices):
        INSTALL = "INSTALL", "Sender angebracht"
        REMOVE = "REMOVE", "Sender entfernt"
        SWAP_SENDER = "SWAP_SENDER", "Sender gewechselt"
        SWAP_BATTERY = "SWAP_BATTERY", "Akku gewechselt"
        PROFILE_CHANGE = "PROFILE_CHANGE", "Profil gewechselt"
        CONFIG_CHANGE = "CONFIG_CHANGE", "Konfiguration geaendert"

    operation_vehicle = models.ForeignKey(OperationVehicle, on_delete=models.CASCADE)
    action_type = models.CharField(max_length=30, choices=ActionType.choices)
    happened_at = models.DateTimeField()
    work_location = models.CharField(max_length=200, blank=True, default="")
    position_on_vehicle = models.CharField(max_length=200, blank=True, default="")
    performed_by = models.ForeignKey(
        Person, on_delete=models.PROTECT,
        related_name="performed",
        limit_choices_to={"role": Person.Role.TECHNICIAN}
    )
    helped_by = models.ManyToManyField(Person, blank=True, related_name="helped")
    sender = models.ForeignKey(GpsSender, on_delete=models.SET_NULL, null=True, blank=True)
    battery_type = models.ForeignKey(BatteryType, on_delete=models.SET_NULL, null=True, blank=True)
    profile_name = models.CharField(max_length=80, blank=True, default="")
    notes = models.TextField(blank=True, default="")
