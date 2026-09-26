from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from accounts.models import Project, ProjectMembership, User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ("id", "username", "email", "first_name", "last_name", "is_active")
        read_only_fields = ("id",)


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    first_name = serializers.CharField(required=False, allow_blank=True, default="")
    last_name = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_username(self, value):
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("Username already exists.")
        return value

    def validate_email(self, value):
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("Email already exists.")
        return value

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class ProjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ("id", "name", "slug", "description", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")


class WorkspaceItemSerializer(serializers.ModelSerializer):
    """Workspace as shown in lists/detail, including whether the requesting
    user can administer it (drives UI affordances)."""

    is_admin = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = ("id", "name", "slug", "description", "is_admin", "archived", "created_at", "updated_at")
        read_only_fields = fields

    def get_is_admin(self, obj) -> bool:
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        if user.is_superuser:
            return True
        return ProjectMembership.objects.filter(
            project=obj, user=user, role=ProjectMembership.Role.ADMIN
        ).exists()


class WorkspaceCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    description = serializers.CharField(required=False, allow_blank=True, default="")


class WorkspaceUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200, required=False)
    description = serializers.CharField(required=False, allow_blank=True)


class MemberAddSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    role = serializers.ChoiceField(choices=ProjectMembership.Role.choices, default=ProjectMembership.Role.VIEWER)


class MemberRoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=ProjectMembership.Role.choices)


# ─── Profile (self-service) ──────────────────────────────────────────────────


class ProfileUpdateSerializer(serializers.Serializer):
    """Self-service profile update. All fields optional; only provided fields
    are changed. Password change requires the current password."""

    first_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    username = serializers.CharField(required=False, max_length=150)
    current_password = serializers.CharField(required=False, style={"input_type": "password"}, write_only=True)
    new_password = serializers.CharField(required=False, style={"input_type": "password"}, write_only=True)

    def validate_username(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Username cannot be empty.")
        if User.objects.filter(username__iexact=value).exclude(pk=self.context["user"].pk).exists():
            raise serializers.ValidationError("Username already exists.")
        return value

    def validate_email(self, value):
        value = (value or "").strip()
        if value and User.objects.filter(email__iexact=value).exclude(pk=self.context["user"].pk).exists():
            raise serializers.ValidationError("Email already exists.")
        return value

    def validate(self, attrs):
        from accounts.services import has_local_password

        user = self.context["user"]
        if "new_password" in attrs and attrs.get("new_password"):
            # SSO users (and anyone without a local password) have nothing to
            # verify against, so they set a password directly.
            if has_local_password(user):
                if not attrs.get("current_password"):
                    raise serializers.ValidationError({"current_password": "Current password is required."})
                if not user.check_password(attrs["current_password"]):
                    raise serializers.ValidationError({"current_password": "Current password is incorrect."})
            validate_password(attrs["new_password"])
        return attrs

    def save(self):
        user = self.context["user"]
        attrs = self.validated_data
        if "first_name" in attrs:
            user.first_name = attrs["first_name"]
        if "last_name" in attrs:
            user.last_name = attrs["last_name"]
        if "email" in attrs:
            user.email = attrs["email"]
        if "username" in attrs:
            user.username = attrs["username"]
        user.save()
        if attrs.get("new_password"):
            user.set_password(attrs["new_password"])
            user.save(update_fields=["password"])
        return user


# ─── Super-admin user management ─────────────────────────────────────────────


class UserCreateSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    first_name = serializers.CharField(required=False, allow_blank=True, default="")
    last_name = serializers.CharField(required=False, allow_blank=True, default="")


class UserAdminUpdateSerializer(serializers.Serializer):
    """Super-admin edit of any user. All fields optional."""

    first_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    last_name = serializers.CharField(required=False, allow_blank=True, max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)
    is_superuser = serializers.BooleanField(required=False)
    password = serializers.CharField(required=False, write_only=True, style={"input_type": "password"})


class ProjectMembershipSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = ProjectMembership
        fields = ("id", "project", "user", "username", "email", "role", "created_at")
        read_only_fields = ("id", "created_at")
