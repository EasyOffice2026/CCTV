"""Admin commands.

    python -m cctv_cloud.cli create-superadmin --email you@example.com --name "Your Name"
    python -m cctv_cloud.cli vapid-keys
    python -m cctv_cloud.cli face-key
    python -m cctv_cloud.cli rotate-face-keys
    python -m cctv_cloud.cli rotate-secret-key --old-key-file FILE
    python -m cctv_cloud.cli security-check [--online]
    python -m cctv_cloud.cli sso-token --issuer <iss> --email <email> [--embed]
    python -m cctv_cloud.cli export-reviewed --tenant <slug> --since 2026-11-01 --out DIR

create-superadmin prints a link to set the password; 2FA is set up at the first login.
vapid-keys prints a new key for browser notifications (CCTV_VAPID_PRIVATE_KEY).
face-key prints a new local key for face templates (CCTV_FACE_KEYS, CCTV_FACE_KEY).
rotate-face-keys re-encrypts face templates that use an older key than CCTV_FACE_KEY.
rotate-secret-key, run after CCTV_SECRET_KEY was changed, re-encrypts 2FA, sign-in
integration and webhook secrets (and face templates without CCTV_FACE_KEY) from the old
key in FILE (or CCTV_OLD_SECRET_KEY) to the new one; see docs/security.md §7.
security-check lists unsafe settings; --online also checks the database and the bucket.
It exits with 1 when there are errors.
sso-token prints a sign-in link as the operational system would make it (HS256
integrations only; for testing an integration end to end).
export-reviewed writes reviewed events (default: false alarms and valid ones) with their
snapshots to DIR for retraining (plan 9.5); every file fetched is in the audit log.
"""

import argparse
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cctv_cloud.api.auth import send_password_link
from cctv_cloud.config import get_settings
from cctv_cloud.db.enums import UserRole
from cctv_cloud.db.models import SsoIntegration, User
from cctv_cloud.db.session import tenant_session
from cctv_cloud.services import security_check, sso
from cctv_cloud.services.face_keys import current_key_id, new_local_key, rotate_templates
from cctv_cloud.services.mailer import LogMailer
from cctv_cloud.services.pilot import export_reviewed
from cctv_cloud.services.storage import get_storage
from cctv_cloud.services.webpush import Vapid, new_vapid_private_key
from cctv_contracts.enums import MediaKind


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


def _rotate_secret_key(old_key_file: str | None) -> int:
    from cctv_cloud.services.secret_rotation import rotate_secret_key

    if old_key_file:
        old_key = Path(old_key_file).read_text(encoding="utf-8").strip()
    else:
        old_key = os.environ.get("CCTV_OLD_SECRET_KEY", "")
    try:
        result = rotate_secret_key(old_key)
    except ValueError as exc:
        print(exc)
        return 1
    if result.unreadable:
        print("nothing changed: these can be decrypted with neither key:")
        for item in result.unreadable:
            print(f"  {item}")
        return 1
    print(
        f"re-encrypted: {result.totp} 2FA secrets, {result.sso} sign-in integration secrets, "
        f"{result.webhooks} webhook secrets, {result.face_templates} face templates "
        f"({result.already_new} already under the new key). Everyone signs in again."
    )
    return 0


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


def _export_reviewed(args: argparse.Namespace) -> int:
    with tenant_session(None) as db:
        tenants = db.execute(text("SELECT tenant_id, slug FROM platform_list_tenants()")).all()
    tenant = next((t.tenant_id for t in tenants if args.tenant in (t.slug, str(t.tenant_id))), None)
    if tenant is None:
        print(f"no tenant {args.tenant!r}")
        return 1

    def moment(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    kinds = {"snapshot": [MediaKind.SNAPSHOT], "clip": [MediaKind.CLIP], "none": []}
    count = export_reviewed(
        get_storage(),
        tenant,
        Path(args.out),
        moment(args.since),
        moment(args.until) if args.until else datetime.now(timezone.utc),
        args.status.split(","),
        kinds.get(args.media, [MediaKind.SNAPSHOT, MediaKind.CLIP]),
        uuid.UUID(args.branch) if args.branch else None,
    )
    print(f"{count} events written to {Path(args.out) / 'manifest.csv'}")
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
    rotate_secret = commands.add_parser("rotate-secret-key")
    rotate_secret.add_argument(
        "--old-key-file", help="file holding the previous key (default: CCTV_OLD_SECRET_KEY)"
    )
    check = commands.add_parser("security-check")
    check.add_argument("--online", action="store_true", help="also check the database and bucket")
    sso_token = commands.add_parser("sso-token")
    sso_token.add_argument("--issuer", required=True)
    sso_token.add_argument("--email", required=True)
    sso_token.add_argument("--embed", action="store_true", help="link to the embedded overview")
    export = commands.add_parser("export-reviewed")
    export.add_argument("--tenant", required=True, help="slug or id")
    export.add_argument("--branch", help="branch id (default: all)")
    export.add_argument("--since", required=True, help="date or time, e.g. 2026-11-01")
    export.add_argument("--until", help="default: now")
    export.add_argument("--status", default="false_alarm,valid", help="review statuses")
    export.add_argument("--media", default="snapshot", choices=["snapshot", "clip", "both", "none"])
    export.add_argument("--out", required=True, help="folder outside the repository")
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
    if args.command == "rotate-secret-key":
        return _rotate_secret_key(args.old_key_file)
    if args.command == "security-check":
        return _security_check(args.online)
    if args.command == "sso-token":
        return _sso_token(args.issuer, args.email, args.embed)
    if args.command == "export-reviewed":
        return _export_reviewed(args)
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
