"""Outbound mail: one adapter, one double, and the rule that it may be absent.

``chaudron.domain.email_ports`` declares what the application asks for. This
package answers it over SMTP, and answers with ``None`` when no mail server is
configured -- which is the normal state of a self-hosted install and therefore
not an error.
"""

from __future__ import annotations

from chaudron.infra.email.smtp import SmtpMailer, SmtpSettings, build_mailer

__all__ = ["SmtpMailer", "SmtpSettings", "build_mailer"]
