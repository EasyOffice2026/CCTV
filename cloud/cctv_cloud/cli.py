"""Admin commands.

    python -m cctv_cloud.cli create-superadmin --email you@example.com --name "Your Name"
    python -m cctv_cloud.cli vapid-keys
    python -m cctv_cloud.cli face-key
    python -m cctv_cloud.cli rotate-face-keys
    python -m cctv_cloud.cli security-check [--online]
    python -m cctv_cloud.cli sso-token --issuer <iss> --email <email> [--embed]

create-superadmin prints a link to set the password; 2FA is set up at the first login.
vapid-keys prints a new key for browser notifications (CCTV_VAPID_PRIVATE_KEY).
face-key prints a new local key for face templates (CCTV_FACE_KEYS, CCTV_FACE_KEY).
rotate-face-keys re-encrypts face templates that use an older key than CCTV_FACE_KEY.
security-check lists unsafe settings; --online also checks the database and the bucket.
It exits with 1 when there are errors.
sso-token prints a sign-in link as the operational system would make it (HS256
integrations only; for testing an integration end to end).
"""

import argparse
import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from cctv_cloud.api.auth import send_password_link
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import UserRole
from cctv_cloud.db.models import SsoIntegration, User
from cctv_cloud.db.session import tenant_session
from cctv_cloud.services import security_check, sso
from cctv_cloud.services.face_keys import current_key_id, new_local_key, rotate_templates
from cctv_cloud.services.mailer import LogMailer
from cctv_cloud.services.storage import get_storage
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


def _security_check(online: bool) -> int:
    findings = security_check.check_settings(get_settings())
    if online:
        findings += security_check.check_database()
        findings += security_check.check_storage(get_storage())
    for f in findings:
        print(f"{f.level.upper():8} {f.topic:11} {f.message}")
    return 1 if any(f.level == "error" for f in findings) else 0


def _sso_token(issuer: str, email: str, embed: bool) -> int:
    try:
        found = sso.find_issuer(issuer)
    except sso.SsoError:
        print(f"no enabled integration with issuer {issuer!r}")
        return 1
    with tenant_session(found.tenant_id) as db:
        integration = db.get_one(SsoIntegration, found.id)
        if integration.algorithm != "HS256":
            print("only HS256 integrations: the private key is the operational system's")
            return 1
        token = sso.sign_in_token(integration, email)
    page = "embed/overview/" if embed else "sso/"
    print(f"{get_settings().web_url.rstrip('/')}/{page}#token={token}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cctv_cloud.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-superadmin")
    create.add_argument("--email", required=True)
    create.add_argument("--name", required=True)
    commands.add_parser("vapid-keys")
    commands.add_parser("face-key")
    commands.add_parser("rotate-face-keys")
    check = commands.add_parser("security-check")
    check.add_argument("--online", action="store_true", help="also check the database and bucket")
    sso_token = commands.add_parser("sso-token")
    sso_token.add_argument("--issuer", required=True)
    sso_token.add_argument("--email", required=True)
    sso_token.add_argument("--embed", action="store_true", help="link to the embedded overview")
    args = parser.parse_args(argv)
    if args.command == "face-key":
        key_id = f"{datetime.now(timezone.utc):%Y-%m}"
        print("# add to CCTV_FACE_KEYS (keep older keys until rotate-face-keys has run):")
        print(json.dumps({key_id: new_local_key()}))
        print(f"CCTV_FACE_KEY=local:{key_id}")
        return 0
    if args.command == "rotate-face-keys":
        from cctv_cloud.worker import tenant_ids

        count = rotate_templates(tenant_ids())
        print(f"{count} face templates re-encrypted with {current_key_id()}")
        return 0
    if args.command == "security-check":
        return _security_check(args.online)
    if args.command == "sso-token":
        return _sso_token(args.issuer, args.email, args.embed)
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
