"""SNS message verification.

AWS SNS posts JSON like:

  {
    "Type": "Notification",            // or "SubscriptionConfirmation"
    "MessageId": "...",
    "TopicArn": "arn:aws:sns:...:...:...",
    "Subject": "Amazon SES Email Receipt",
    "Message": "<raw-email-or-json>",
    "Timestamp": "2026-05-09T12:00:00.000Z",
    "SignatureVersion": "1",           // or "2" for SHA-256
    "Signature": "<base64>",
    "SigningCertURL": "https://sns.<region>.amazonaws.com/SimpleNotificationService-XXXX.pem",
    "SubscribeURL": "https://sns...",  // SubscriptionConfirmation only
    "Token": "..."                     // SubscriptionConfirmation only
  }

We verify the signature against the AWS-published certificate so that an
attacker can't post arbitrary JSON to our public webhook URL. The cert URL
is pinned to the `sns.<region>.amazonaws.com` host family.

Reference: https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate

logger = logging.getLogger(__name__)

# AWS regional SNS hosts; we accept any of these but reject any other host as
# a defense against a forged SigningCertURL pointing at attacker-controlled storage.
_SIGNING_HOST_RE = re.compile(r"^sns(?:\.[a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?$")
_TOPIC_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws|aws-cn|aws-us-gov):sns:"
    r"(?P<region>[a-z0-9](?:[a-z0-9-]{1,30}[a-z0-9])?):"
    r"(?P<account_id>[0-9]{12}):"
    r"(?P<topic>(?:[A-Za-z0-9_-]{1,256}|[A-Za-z0-9_-]{1,251}\.fifo))$"
)


# Headers required for the canonical signing string for each message type.
# See AWS docs link above.
_NOTIFICATION_KEYS = (
    "Message",
    "MessageId",
    "Subject",
    "Timestamp",
    "TopicArn",
    "Type",
)
_SUBSCRIPTION_KEYS = (
    "Message",
    "MessageId",
    "SubscribeURL",
    "Timestamp",
    "Token",
    "TopicArn",
    "Type",
)


class SNSVerificationError(Exception):
    """Raised when an SNS message fails signature verification."""


class _RejectRedirects(HTTPRedirectHandler):
    """Keep an AWS-pinned URL from redirecting provider I/O elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_without_redirects(url: str, *, timeout: float):
    return build_opener(_RejectRedirects()).open(url, timeout=timeout)


@dataclass(frozen=True)
class SNSTopicArn:
    arn: str
    partition: str
    region: str
    account_id: str
    topic: str

    @property
    def hostname(self) -> str:
        suffix = "amazonaws.com.cn" if self.partition == "aws-cn" else "amazonaws.com"
        return f"sns.{self.region}.{suffix}"


def parse_sns_topic_arn(value: object) -> SNSTopicArn:
    """Parse the narrow SNS Topic ARN form accepted for mailbox bindings."""

    arn = str(value or "").strip()
    match = _TOPIC_ARN_RE.fullmatch(arn)
    if match is None:
        raise SNSVerificationError("SNS TopicArn is not a valid topic ARN")
    values = match.groupdict()
    partition = values["partition"]
    region = values["region"]
    if partition == "aws-cn" and not region.startswith("cn-"):
        raise SNSVerificationError("SNS TopicArn partition and region do not match")
    if partition == "aws-us-gov" and not region.startswith("us-gov-"):
        raise SNSVerificationError("SNS TopicArn partition and region do not match")
    if partition == "aws" and (
        region.startswith("cn-") or region.startswith("us-gov-")
    ):
        raise SNSVerificationError("SNS TopicArn partition and region do not match")
    return SNSTopicArn(arn=arn, **values)


def _assert_topic_url(url: object, topic: SNSTopicArn, *, field: str) -> None:
    try:
        parsed = urlparse(str(url or ""))
        port = parsed.port
    except ValueError as exc:
        raise SNSVerificationError(f"{field} is not a valid URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != topic.hostname
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise SNSVerificationError(f"{field} does not match the bound SNS region")


def validate_sns_topic_binding(payload: dict, expected_topic_arn: object) -> None:
    """Bind one signed SNS payload to the mailbox's exact topic/account/region.

    An SNS signature proves that AWS signed the message, not that the message
    came from a topic owned by this application.  The exact ARN comparison is
    therefore required before accepting notifications or following a
    subscription-confirmation URL.
    """

    expected = parse_sns_topic_arn(expected_topic_arn)
    supplied = parse_sns_topic_arn(payload.get("TopicArn"))
    if supplied.arn != expected.arn:
        raise SNSVerificationError("SNS TopicArn does not match this mailbox")
    _assert_topic_url(payload.get("SigningCertURL"), expected, field="SigningCertURL")
    if payload.get("Type") == "SubscriptionConfirmation":
        _assert_topic_url(payload.get("SubscribeURL"), expected, field="SubscribeURL")


def _build_string_to_sign(payload: dict, keys: Iterable[str]) -> bytes:
    """Build the AWS-defined canonical string for signing.

    Format: for each key in `keys` (alphabetical order), if the key is present
    in the payload, append "<key>\\n<value>\\n".
    """
    pieces: list[str] = []
    for key in keys:
        if key in payload and payload[key] is not None:
            pieces.append(key)
            pieces.append("\n")
            pieces.append(str(payload[key]))
            pieces.append("\n")
    return "".join(pieces).encode("utf-8")


def _fetch_signing_cert(url: str, *, timeout: float = 5.0) -> bytes:
    """Fetch the SNS signing certificate, with hostname pinning."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SNSVerificationError("SigningCertURL must use HTTPS")
    if not _SIGNING_HOST_RE.match(parsed.hostname or ""):
        raise SNSVerificationError("SigningCertURL host is not an AWS SNS host")
    if not parsed.path.endswith(".pem"):
        raise SNSVerificationError("SigningCertURL must reference a PEM certificate")
    with _open_without_redirects(url, timeout=timeout) as response:
        # The default urllib client follows redirects.  Disabling them keeps a
        # trusted-looking AWS URL from becoming provider-side SSRF, while this
        # final URL check also protects alternate/injected fetch transports.
        final_url = response.geturl()
        final = urlparse(final_url)
        if (
            final.scheme != "https"
            or final.hostname != parsed.hostname
            or final.port not in {None, 443}
            or final.username is not None
            or final.password is not None
            or not final.path.endswith(".pem")
        ):
            raise SNSVerificationError("Signing certificate URL changed during fetch")
        return response.read()


def _hash_alg_for_version(version: str) -> hashes.HashAlgorithm:
    if version == "1":
        return hashes.SHA1()
    if version == "2":
        return hashes.SHA256()
    raise SNSVerificationError(f"Unsupported SignatureVersion: {version!r}")


def verify_sns_message(
    payload: dict,
    *,
    fetch_cert=_fetch_signing_cert,
) -> None:
    """Verify the signature on an SNS payload.

    Raises `SNSVerificationError` if anything looks wrong. The `fetch_cert`
    seam exists so tests can substitute a deterministic cert without going to
    the network.
    """
    msg_type = payload.get("Type")
    if msg_type not in {
        "Notification",
        "SubscriptionConfirmation",
        "UnsubscribeConfirmation",
    }:
        raise SNSVerificationError(f"Unknown SNS Type: {msg_type!r}")

    signature_b64 = payload.get("Signature")
    cert_url = payload.get("SigningCertURL")
    sig_version = str(payload.get("SignatureVersion", ""))
    if not signature_b64 or not cert_url or not sig_version:
        raise SNSVerificationError("Missing signature fields")

    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise SNSVerificationError("Signature is not valid base64") from exc

    try:
        cert_bytes = fetch_cert(cert_url)
        cert = load_pem_x509_certificate(cert_bytes)
        public_key = cert.public_key()
    except SNSVerificationError:
        raise
    except Exception:
        # urllib errors frequently embed their full URL. Normalize them before
        # they reach the view or application logging.
        raise SNSVerificationError("Could not load SNS signing certificate") from None

    keys = _NOTIFICATION_KEYS if msg_type == "Notification" else _SUBSCRIPTION_KEYS
    string_to_sign = _build_string_to_sign(payload, keys)
    hash_alg = _hash_alg_for_version(sig_version)

    try:
        # SNS signs with PKCS#1 v1.5 RSA.
        public_key.verify(
            signature,
            string_to_sign,
            padding.PKCS1v15(),
            hash_alg,
        )
    except InvalidSignature as exc:
        raise SNSVerificationError("Signature does not match") from exc
    except Exception as exc:  # pragma: no cover — non-RSA cert is unexpected
        raise SNSVerificationError("SNS signature verification failed") from exc


def confirm_subscription(
    payload: dict,
    *,
    expected_topic_arn: object | None = None,
    fetch=_open_without_redirects,
    timeout: float = 5.0,
) -> None:
    """If the payload is a SubscriptionConfirmation, hit the SubscribeURL once.

    SNS will GET this URL itself when the topic is wired through the AWS console;
    confirming programmatically is convenient when an admin pastes the webhook
    URL directly into the SES Receipt Rule and lets SNS auto-subscribe.
    """
    if payload.get("Type") != "SubscriptionConfirmation":
        return
    if expected_topic_arn is None:
        # Preserve the optional argument at the Python API boundary while
        # making legacy call sites fail closed instead of confirming whichever
        # AWS topic supplied the signed message.
        raise SNSVerificationError("Expected SNS topic binding is required")
    # Defense in depth: every confirmation call must bind the exact tenant
    # topic even if this helper is later invoked from a new code path.
    validate_sns_topic_binding(payload, expected_topic_arn)
    url = payload.get("SubscribeURL")
    if not url:
        raise SNSVerificationError("SubscriptionConfirmation missing SubscribeURL")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not _SIGNING_HOST_RE.match(parsed.hostname or ""):
        raise SNSVerificationError("SubscribeURL is not on an AWS SNS host")
    try:
        with fetch(url, timeout=timeout) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            _assert_topic_url(
                final_url,
                parse_sns_topic_arn(expected_topic_arn),
                field="SubscribeURL",
            )
            response.read()
    except Exception as exc:
        # Never include the signed confirmation URL or its Token query value in
        # logs or propagated exception text.
        logger.warning(
            "Failed to confirm SNS subscription error_type=%s",
            type(exc).__name__,
        )
        raise SNSVerificationError("SNS subscription confirmation failed") from None
