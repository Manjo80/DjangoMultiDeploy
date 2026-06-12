from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("control", "0002_add_favorite_command"),
    ]

    operations = [
        migrations.AlterField(
            model_name="userprofile",
            name="totp_secret",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
