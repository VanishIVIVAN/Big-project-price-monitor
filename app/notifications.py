"""Notification channels. The tracker works with the Notifier interface only,
so channels can be added or replaced without changing the rest of the code."""
from __future__ import annotations

import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage

from .config import Config
from .domain import User
from .storage import Storage


class NotificationError(Exception):
    """A notification could not be delivered."""


class Notifier(ABC):
    @abstractmethod
    def send(self, user: User, product_id: int, message: str) -> None:
        """Deliver the message to the user. Raise NotificationError on failure."""


class InAppNotifier(Notifier):
    """Stores the message so the user sees it on the site."""

    def __init__(self, storage: Storage):
        self.storage = storage

    def send(self, user: User, product_id: int, message: str) -> None:
        self.storage.add_notification(user.id, product_id, message)


class EmailNotifier(Notifier):
    """Sends the message by e-mail if the user left an address."""

    def __init__(self, host: str, port: int, sender: str,
                 username: str = "", password: str = ""):
        self.host, self.port, self.sender = host, port, sender
        self.username, self.password = username, password

    @classmethod
    def from_config(cls, config: Config) -> "EmailNotifier | None":
        if not config.smtp_host:
            return None
        sender = config.smtp_from or config.smtp_user
        if not sender:
            return None
        return cls(config.smtp_host, config.smtp_port, sender,
                   config.smtp_user, config.smtp_password)

    def send(self, user: User, product_id: int, message: str) -> None:
        if not user.email:
            return
        mail = EmailMessage()
        mail["Subject"] = "Знижка на товар зі списку бажаного"
        mail["From"] = self.sender
        mail["To"] = user.email
        mail.set_content(message)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=10) as smtp:
                smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(mail)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotificationError(f"Не вдалося надіслати лист: {exc}") from exc


class CompositeNotifier(Notifier):
    """Sends through all channels; a failing channel does not block the others."""

    def __init__(self, notifiers: list[Notifier]):
        self.notifiers = notifiers
        self.failures: list[str] = []

    def send(self, user: User, product_id: int, message: str) -> None:
        for notifier in self.notifiers:
            try:
                notifier.send(user, product_id, message)
            except NotificationError as exc:
                self.failures.append(str(exc))
