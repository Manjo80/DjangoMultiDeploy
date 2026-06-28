# Generated for the configurable update-pipeline feature.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("control", "0004_healthsample_notificationsettings"),
    ]

    operations = [
        migrations.CreateModel(
            name="UpdateCommand",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("project_name", models.CharField(max_length=100)),
                (
                    "label",
                    models.CharField(
                        help_text='Beschreibung, z.B. "Glossar laden"',
                        max_length=80,
                    ),
                ),
                (
                    "command",
                    models.CharField(
                        help_text='manage.py Unterbefehl, z.B. "load_glossary" oder "loaddata seed.json"',
                        max_length=500,
                    ),
                ),
                (
                    "order",
                    models.IntegerField(
                        default=0, help_text="Reihenfolge (aufsteigend)"
                    ),
                ),
                (
                    "enabled",
                    models.BooleanField(
                        default=True,
                        help_text="Deaktivierte Befehle werden beim Update übersprungen.",
                    ),
                ),
            ],
            options={
                "ordering": ["project_name", "order", "label"],
                "unique_together": {("project_name", "command")},
            },
        ),
    ]
