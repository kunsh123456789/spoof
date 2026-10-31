"""Friendly, structured errors that the API returns to the UI."""
from __future__ import annotations

TUNNELD_CMD = "sudo .venv/bin/python -m pymobiledevice3 remote tunneld"


class AppError(Exception):
    """An error with a stable code, a human message and an optional fix hint."""

    status = 400

    def __init__(self, code: str, message: str, hint: str | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        if status is not None:
            self.status = status

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint}


def no_device() -> AppError:
    return AppError(
        "no_device",
        "No iPhone found.",
        "Plug the iPhone in with a USB cable, unlock it, and make sure the cable carries data.",
        status=503,
    )


def not_trusted() -> AppError:
    return AppError(
        "not_trusted",
        "This computer is not trusted by the iPhone.",
        "Unlock the iPhone and tap \"Trust\" on the prompt, or press \"Trust this computer\" below.",
        status=409,
    )


def developer_mode_off() -> AppError:
    return AppError(
        "developer_mode_off",
        "Developer Mode is off on the iPhone.",
        "On the iPhone: Settings > Privacy & Security > Developer Mode > On, then restart and confirm. "
        "(The option only appears after the phone has been connected to a computer once.)",
        status=409,
    )


def tunnel_needed() -> AppError:
    return AppError(
        "tunnel_needed",
        "iOS 17+ needs a developer tunnel and none could be started automatically.",
        f"In a separate terminal, from the project folder, run:  {TUNNELD_CMD}  (leave it running), then try again.",
        status=503,
    )


def timeout() -> AppError:
    return AppError(
        "connection_lost",
        "The iPhone stopped answering.",
        "Check the cable or Wi-Fi, and that the iPhone is awake and unlocked. The app will try again.",
        status=503,
    )
