"""Admin commands.

    python -m cctv_cloud.cli create-superadmin --email you@example.com --name "Your Name"
    python -m cctv_cloud.cli vapid-keys

create-superadmin prints a link to set the password; 2FA is set up at the first login.
vapid-keys prints a new key for browser notifications (CCTV_VAPID_PRIVATE_KEY).
"""

import argparse
import uuid

from sqlalchemy.exc import IntegrityError

from cctv_cloud.api.auth import send_password_link
from cctv_cloud.db.enums import UserRole
from cctv_cloud.db.models import User
from cctv_cloud.db.session import tenant_session
from cctv_cloud.services.mailer import LogMailer
from cctv_cloud.services.webpush import Vapid, new_vapid_private_key


class _PrintMailer(LogMailer):
    def send(self, to: str, subject: str, body: str) -> None:
        print(f"{subject} ({to}):\n{body}")


def create_superadmin(email: str, name: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    # Super-admins have no tenant; RLS allows the row because app.user_id is its id.
    with tenant_session(None, user_id) as db:
        user = User(
            id=user_id, email=email.lower(), full_name=name, role=UserRole.SUPER_ADMIN.value
        )
        db.add(user)
        db.flush()
        send_password_link(user, _PrintMailer(), purpose="invite")
    return user_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cctv_cloud.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-superadmin")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    commands.add_parser("vapid-keys")
    args = parser.parse_args(argv)
    if args.command == "vapid-keys":
        private = new_vapid_private_key()
        print(f"CCTV_VAPID_PRIVATE_KEY={private}")
        print(f"# public key (sent to browsers): {Vapid.from_private(private, '').public_key}")
        return 0
    try:
        create_superadmin(args.email, args.name)
    except IntegrityError:
        print("a user with this email already exists")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
