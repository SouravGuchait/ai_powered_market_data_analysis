from rest_framework.permissions import BasePermission


class IsOwner(BasePermission):
    message = "You do not have access to this object."

    def has_object_permission(self, request, view, obj):
        return getattr(obj, "owner_id", None) == request.user.pk
