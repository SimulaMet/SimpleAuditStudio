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
        fields = ("id", "name", "slug", "description", "is_admin", "created_at", "updated_at")
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


class ProjectMembershipSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = ProjectMembership
        fields = ("id", "project", "user", "username", "email", "role", "created_at")
        read_only_fields = ("id", "created_at")
