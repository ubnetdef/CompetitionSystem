import base64
import ftplib
import hashlib
import hmac
import io
import ipaddress
import logging
import shutil
import socket
import ssl
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import dns.exception
import dns.resolver
import ldap3
import paramiko
import pymysql
import requests
import winrm
from winrm.protocol import Protocol

from . import create_app
from .check_secrets import decrypt_credentials
from .extensions import db
from .models import (
    BankTransaction,
    Competition,
    Host,
    ScoreResult,
    ScoreRound,
    Service,
    Team,
    TeamServiceConfig,
)

logger = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 65536


class _ResponseTooLarge(Exception):
    pass


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, new_url):
        return None


def _format_address(address):
    value = address.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        parsed = ipaddress.ip_address(value)
        return f"[{value}]" if parsed.version == 6 else value
    except ValueError:
        if not value or any(char in value for char in "/?#@"):
            raise ValueError("Host must be a hostname or IP address.")
        return value


def _probe(job):
    started = time.monotonic()
    try:
        check_type = job.get("check_type")
        config = {
            **(job.get("check_config") or {}),
            "credential_key": job.get("credential_key"),
            "secret_key": job.get("secret_key"),
        }
        if check_type == "ping":
            return _ping(job, config, started)
        if check_type == "ssh":
            return _ssh(job, config, started)
        if check_type == "winrm":
            return _winrm(job, config, started)
        if check_type == "ldap":
            return _ldap(job, config, started)
        if check_type == "sql":
            return _mysql(job, config, started)
        if check_type == "ftp":
            return _ftps(job, config, started)
        if check_type == "dns":
            resolver = dns.resolver.Resolver(configure=True)
            target_address = ""
            if config.get("query_host") or "{address}" in config["expected_output"]:
                if not job["address"]:
                    raise ValueError("This DNS check needs a host address for its device group.")
                target_address = _format_address(job["address"]).strip("[]")
            if config.get("query_host"):
                resolver.nameservers = [target_address]
                resolver.port = job["port"] or 53
            answer = resolver.resolve(
                config["lookup"],
                config["record_type"],
                lifetime=job["timeout"],
                search=False,
            )
            expected = (
                config["expected_output"]
                .replace("{address}", target_address)
                .strip()
                .strip('"')
                .casefold()
            )
            actual = {record.to_text().strip().strip('"').casefold() for record in answer}
            elapsed = int((time.monotonic() - started) * 1000)
            if expected not in actual:
                return False, "DNS lookup completed, but the expected answer was not present.", elapsed
            return True, f"DNS {config['record_type']} check passed.", elapsed

        host = _format_address(job["address"])
        if job.get("check_type") == "tcp" or job["protocol"] == "tcp":
            with socket.create_connection(
                (host.strip("[]"), job["port"]), timeout=job["timeout"]
            ):
                return True, "TCP connection succeeded.", int((time.monotonic() - started) * 1000)

        path = job["path"] if job["path"].startswith("/") else f"/{job['path']}"
        scheme = config.get(
            "scheme",
            job["protocol"] if job["protocol"] in {"http", "https"} else "http",
        )
        target = host
        subdomain = config.get("subdomain", "").strip()
        if subdomain:
            try:
                ipaddress.ip_address(host.strip("[]"))
            except ValueError:
                pass
            else:
                raise ValueError("A subdomain check requires a hostname, not an IP address.")
            target = f"{subdomain}.{host}"
        url = f"{scheme}://{target}:{job['port']}{path}"
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
            raise ValueError("The configured HTTP target is invalid.")
        request = Request(url, headers={"User-Agent": "CompetitionSystem-Scorer/1.0"})
        opener = build_opener(NoRedirectHandler())
        try:
            response = opener.open(request, timeout=job["timeout"])
            status = response.status
            body = response.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
            response.close()
        except HTTPError as error:
            status = error.code
            body = error.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
            error.close()
        if status != job["expected_status"]:
            return False, f"Expected HTTP {job['expected_status']}; received HTTP {status}.", int((time.monotonic() - started) * 1000)
        if job["expected_text"] and job["expected_text"] not in body:
            return False, "The response did not contain the configured text.", int((time.monotonic() - started) * 1000)
        return True, f"HTTP {status} check passed.", int((time.monotonic() - started) * 1000)
    except (socket.timeout, TimeoutError):
        return False, f"Check timed out after {job['timeout']} seconds.", int((time.monotonic() - started) * 1000)
    except dns.exception.Timeout:
        return False, f"DNS lookup timed out after {job['timeout']} seconds.", int((time.monotonic() - started) * 1000)
    except dns.resolver.NXDOMAIN:
        return False, "DNS lookup failed: the configured name does not exist.", int((time.monotonic() - started) * 1000)
    except (dns.resolver.NoAnswer, dns.resolver.NoNameservers) as error:
        return False, f"DNS lookup failed: {str(error)[:300]}", int((time.monotonic() - started) * 1000)
    except (OSError, URLError, ValueError) as error:
        return False, f"Connection check failed: {str(error)[:350]}", int((time.monotonic() - started) * 1000)


def _credentials(config):
    credential_values = config.get("credential_values")
    if credential_values is not None:
        if (
            not isinstance(credential_values, (tuple, list))
            or len(credential_values) != 2
            or not all(isinstance(value, str) for value in credential_values)
        ):
            raise ValueError("Team-specific check credentials are malformed.")
        return credential_values[0], credential_values[1]
    token = config.get("credentials")
    if not token:
        raise ValueError("Check credentials are missing. Reconfigure this service check.")
    return decrypt_credentials(
        token,
        credential_key=config.get("credential_key"),
        secret_key=config.get("secret_key"),
    )


def _ping(job, config, started):
    host = _format_address(job["address"]).strip("[]")
    executable = shutil.which("ping")
    if executable is None:
        return False, "PING is unavailable: the scoring image does not include the ping utility.", 0
    attempts = max(1, min(int(config.get("attempts", 1)), 4))
    timeout = max(1, min(int(job["timeout"]), 30))
    try:
        result = subprocess.run(
            [executable, "-n", "-c", str(attempts), "-W", str(timeout), "--", host],
            capture_output=True,
            text=True,
            timeout=timeout * attempts + 2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"PING timed out after {timeout * attempts + 2} seconds.", int(
            (time.monotonic() - started) * 1000
        )
    elapsed = int((time.monotonic() - started) * 1000)
    if result.returncode == 0:
        return True, f"PING received a reply from {host}.", elapsed
    return False, f"PING received no reply from {host}.", elapsed


def _ssh(job, config, started):
    transport = None
    try:
        username, password = _credentials(config)
        host = _format_address(job["address"]).strip("[]")
        sock = socket.create_connection(
            (host, job["port"]), timeout=job["timeout"]
        )
        transport = paramiko.Transport(sock)
        transport.start_client(timeout=job["timeout"])
        key = transport.get_remote_server_key()
        fingerprint = "SHA256:" + base64.b64encode(
            hashlib.sha256(key.asbytes()).digest()
        ).decode("ascii").rstrip("=")
        expected_fingerprint = config.get("host_key_fingerprint")
        if expected_fingerprint:
            if not hmac.compare_digest(fingerprint, expected_fingerprint):
                return False, "SSH host-key fingerprint does not match the configured value.", int(
                    (time.monotonic() - started) * 1000
                )
        elif config.get("host_key_verification") != "skip":
            return False, "SSH host-key verification is not configured.", int(
                (time.monotonic() - started) * 1000
            )
        transport.auth_password(username, password, fallback=False)
        if not transport.is_authenticated():
            return False, "SSH authentication was not accepted.", int(
                (time.monotonic() - started) * 1000
            )
        if config.get("command"):
            output = _ssh_run_command(transport, config["command"], job["timeout"])
            if config["expected_output"].strip().casefold() not in output.casefold():
                return False, "SSH command ran, but the expected output was not found.", int(
                    (time.monotonic() - started) * 1000
                )
            return True, "SSH authentication and command output check passed.", int(
                (time.monotonic() - started) * 1000
            )
        verification_result = (
            "SSH password authentication passed; host key was not verified."
            if not expected_fingerprint
            else "SSH host key and password authentication checks passed."
        )
        return True, verification_result, int(
            (time.monotonic() - started) * 1000
        )
    except paramiko.AuthenticationException:
        return False, "SSH authentication was rejected by the server.", int(
            (time.monotonic() - started) * 1000
        )
    except (paramiko.SSHException, OSError, KeyError, ValueError) as error:
        return False, f"SSH check failed: {str(error)[:300]}", int(
            (time.monotonic() - started) * 1000
        )
    finally:
        if transport is not None:
            transport.close()


def _ssh_run_command(transport, command, timeout):
    channel = transport.open_session(timeout=timeout)
    try:
        channel.settimeout(timeout)
        channel.exec_command(command)
        output = io.BytesIO()
        while output.tell() < MAX_RESPONSE_BYTES:
            chunk = channel.recv(4096)
            if not chunk:
                break
            output.write(chunk)
        return output.getvalue().decode("utf-8", errors="replace")
    finally:
        channel.close()


def _winrm(job, config, started):
    protocol = None
    shell_id = None
    succeeded = False
    try:
        username, password = _credentials(config)
        host = _format_address(job["address"])
        scheme = config.get("scheme", "https")
        endpoint = f"{scheme}://{host}:{job['port']}/wsman"
        timeout = max(1, min(int(job["timeout"]), 30))
        protocol = Protocol(
            endpoint=endpoint,
            transport=config["auth_transport"],
            username=username,
            password=password,
            server_cert_validation="validate",
            operation_timeout_sec=timeout,
            read_timeout_sec=timeout + 2,
        )
        shell_id = protocol.open_shell()
        if not shell_id:
            raise ValueError("WinRM server did not create a shell.")
        succeeded = True
        if config.get("command"):
            command_id = protocol.run_command(shell_id, config["command"])
            try:
                stdout, _stderr, _code = protocol.get_command_output(shell_id, command_id)
            finally:
                protocol.cleanup_command(shell_id, command_id)
            output = stdout[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace")
            if config["expected_output"].strip().casefold() not in output.casefold():
                return False, "WinRM command ran, but the expected output was not found.", int(
                    (time.monotonic() - started) * 1000
                )
            return True, "WinRM authentication and command output check passed.", int(
                (time.monotonic() - started) * 1000
            )
    except winrm.exceptions.InvalidCredentialsError:
        return False, "WinRM credentials were rejected by the server.", int(
            (time.monotonic() - started) * 1000
        )
    except (
        winrm.exceptions.WinRMOperationTimeoutError,
        requests.exceptions.Timeout,
    ):
        return False, f"WinRM authentication timed out after {job['timeout']} seconds.", int(
            (time.monotonic() - started) * 1000
        )
    except (
        winrm.exceptions.WinRMError,
        requests.exceptions.RequestException,
        OSError,
        KeyError,
        ValueError,
    ) as error:
        logger.info("WinRM authentication check failed: %s", error)
        return False, "WinRM HTTPS authentication failed; check the endpoint, certificate, and transport settings.", int(
            (time.monotonic() - started) * 1000
        )
    finally:
        if protocol is not None and shell_id is not None:
            try:
                protocol.close_shell(shell_id)
            except (
                winrm.exceptions.WinRMError,
                requests.exceptions.RequestException,
                OSError,
            ) as error:
                logger.warning("Could not close the temporary WinRM shell: %s", error)
                succeeded = False
    if not succeeded:
        return False, "WinRM could not open and close a temporary shell.", int(
            (time.monotonic() - started) * 1000
        )
    return True, "WinRM HTTPS credentials were accepted; the temporary shell was closed without running a command.", int(
        (time.monotonic() - started) * 1000
    )


def _ldap(job, config, started):
    connection = None
    try:
        username, password = _credentials(config)
        host = _format_address(job["address"]).strip("[]")
        use_ssl = config.get("use_ssl", True)
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, valid_names=[host]) if use_ssl else None
        server = ldap3.Server(
            host,
            port=job["port"],
            use_ssl=use_ssl,
            tls=tls,
            get_info=ldap3.NONE,
            connect_timeout=job["timeout"],
        )
        domain = config.get("domain")
        if domain and not any(char in username for char in "@\\="):
            username = f"{username}@{domain}"
        connection = ldap3.Connection(
            server,
            user=username,
            password=password,
            auto_bind=True,
            receive_timeout=job["timeout"],
        )
        succeeded = connection.search(
            search_base=config["base_dn"],
            search_filter=config["search_filter"],
            search_scope=ldap3.SUBTREE,
            attributes=config["attributes"],
            size_limit=20,
            time_limit=job["timeout"],
        )
        expected = config.get("expected_output", "").casefold()
        if expected:
            for entry in connection.entries:
                if expected in entry.entry_to_json().casefold():
                    return True, "LDAP bind and expected read-only search result passed.", int(
                        (time.monotonic() - started) * 1000
                    )
        elif succeeded and connection.entries:
            return True, "LDAP bind and read-only search returned results.", int(
                (time.monotonic() - started) * 1000
            )
        if not succeeded:
            return False, "LDAP search failed or exceeded its configured result limits.", int(
                (time.monotonic() - started) * 1000
            )
        return False, "LDAP search completed, but the expected value was not found.", int(
            (time.monotonic() - started) * 1000
        )
    except ldap3.core.exceptions.LDAPExceptionError as error:
        return False, f"LDAP check failed: {str(error)[:300]}", int(
            (time.monotonic() - started) * 1000
        )
    except (OSError, ssl.SSLError, KeyError, ValueError) as error:
        return False, f"LDAP connection failed: {str(error)[:300]}", int(
            (time.monotonic() - started) * 1000
        )
    finally:
        if connection is not None:
            connection.unbind()


def _mysql(job, config, started):
    connection = None
    try:
        username, password = _credentials(config)
        host = _format_address(job["address"]).strip("[]")
        connection = pymysql.connect(
            host=host,
            port=job["port"],
            user=username,
            password=password,
            database=config["database_name"],
            connect_timeout=job["timeout"],
            read_timeout=job["timeout"],
            write_timeout=job["timeout"],
            autocommit=False,
            ssl=ssl.create_default_context() if config.get("use_tls", True) else None,
        )
        if config.get("command"):
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute(config["command"])
                row_count = len(cursor.fetchmany(int(config["max_rows"]) + 1))
            if int(config["min_rows"]) <= row_count <= int(config["max_rows"]):
                return True, f"MySQL query returned {row_count} rows, within the expected range.", int(
                    (time.monotonic() - started) * 1000
                )
            return False, f"MySQL query returned {row_count}+ rows; expected {config['min_rows']}-{config['max_rows']}.", int(
                (time.monotonic() - started) * 1000
            )
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            row = cursor.fetchone()
        if row == (1,):
            return True, "MySQL TLS authentication and read-only health query passed.", int(
                (time.monotonic() - started) * 1000
            )
        return False, "MySQL health query returned an unexpected result.", int(
            (time.monotonic() - started) * 1000
        )
    except (pymysql.MySQLError, OSError, KeyError, ValueError) as error:
        return False, f"MySQL check failed: {str(error)[:300]}", int(
            (time.monotonic() - started) * 1000
        )
    finally:
        if connection is not None:
            connection.close()


def _validate_remote_filename(filename):
    if (
        not filename
        or len(filename) > 255
        or "\x00" in filename
        or ".." in filename.split("/")
        or any(ord(char) < 32 or ord(char) == 127 for char in filename)
    ):
        raise ValueError("The configured FTP filename is invalid.")


def _ftps(job, config, started):
    connection = None
    try:
        username, password = _credentials(config)
        filename = config["read_filename"]
        if (
            not filename
            or len(filename) > 255
            or "\x00" in filename
            or ".." in filename.split("/")
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        ):
            raise ValueError("The configured FTPS filename is invalid.")
        host = _format_address(job["address"]).strip("[]")
        if config.get("use_tls", True):
            connection = ftplib.FTP_TLS(
                context=ssl.create_default_context(),
                timeout=job["timeout"],
            )
            connection.connect(host, job["port"])
            connection.auth()
            connection.login(username, password)
            connection.prot_p()
        else:
            connection = ftplib.FTP(timeout=job["timeout"])
            connection.connect(host, job["port"])
            connection.login(username, password)
        write_filename = config.get("write_filename")
        if write_filename:
            _validate_remote_filename(write_filename)
            connection.storbinary(
                f"STOR {write_filename}",
                io.BytesIO(config.get("write_text", "").encode("utf-8")),
            )
        content = io.BytesIO()

        def append_response_chunk(chunk):
            if content.tell() + len(chunk) > MAX_RESPONSE_BYTES:
                raise _ResponseTooLarge
            content.write(chunk)

        connection.retrbinary(
            f"RETR {filename}",
            append_response_chunk,
            blocksize=8192,
        )
        expected = config["expected_output"].casefold()
        if expected in content.getvalue().decode("utf-8", errors="replace").casefold():
            return True, "FTPS TLS login and read-only file check passed.", int(
                (time.monotonic() - started) * 1000
            )
        return False, "FTPS file was read, but the expected text was not found.", int(
            (time.monotonic() - started) * 1000
        )
    except _ResponseTooLarge:
        return False, "FTPS file exceeds the 65,536-byte scoring limit.", int(
            (time.monotonic() - started) * 1000
        )
    except (ftplib.Error, OSError, ssl.SSLError, KeyError, ValueError) as error:
        return False, f"FTPS read check failed: {str(error)[:300]}", int(
            (time.monotonic() - started) * 1000
        )
    finally:
        if connection is not None:
            connection.close()


def process_scoring_round(round_id, flask_app=None, claimed=False):
    app = flask_app or create_app()
    with app.app_context():
        score_round = db.session.get(ScoreRound, round_id)
        if score_round is None:
            logger.error("Scoring round %s does not exist.", round_id)
            return
        expected_status = "running" if claimed else "queued"
        if score_round.status != expected_status:
            logger.info("Skipping scoring round %s with status %s.", round_id, score_round.status)
            return

        competition = db.session.get(Competition, score_round.competition_id)
        if competition is None or competition.status != "active":
            score_round.status = "failed"
            score_round.error_message = "Competition was no longer active when this round began."
            score_round.completed_at = datetime.now(timezone.utc)
            db.session.commit()
            return

        score_round.status = "running"
        if score_round.started_at is None:
            score_round.started_at = datetime.now(timezone.utc)
        payout_per_point = competition.payout_per_point
        teams = list(competition.teams)
        hosts = {
            (host.team_id, host.host_group_id): host.address
            for host in Host.query.filter_by(competition_id=competition.id).all()
        }
        service_models = Service.query.filter_by(
            competition_id=competition.id,
            enabled=True,
            paused=False,
        ).all()
        services = [
            {
                "id": service.id,
                "name": service.name,
                "check_type": service.check_type or service.protocol,
                "check_config": service.check_config or {},
                "team_editable_fields": set(service.team_editable_fields or []),
                "protocol": service.protocol,
                "port": service.port,
                "path": service.path,
                "expected_status": service.expected_status,
                "expected_text": service.expected_text,
                "points": service.points,
                "points_boost": service.points_boost,
                "host_group_id": service.host_group_id,
                "timeout": service.timeout_seconds,
                "credential_key": app.config.get("CHECK_CREDENTIAL_KEY"),
                "secret_key": app.config.get("SECRET_KEY"),
            }
            for service in service_models
        ]
        team_ids = [team.id for team in teams]
        service_ids = [service.id for service in service_models]
        team_configurations = {}
        if team_ids and service_ids:
            team_configurations = {
                (configuration.team_id, configuration.service_id): (
                    configuration.credentials_override
                )
                for configuration in TeamServiceConfig.query.filter(
                    TeamServiceConfig.team_id.in_(team_ids),
                    TeamServiceConfig.service_id.in_(service_ids),
                ).all()
            }
        jobs = []
        for team in teams:
            for service in services:
                check_config = dict(service["check_config"])
                override_token = team_configurations.get((team.id, service["id"]))
                editable_fields = service["team_editable_fields"]
                if override_token and editable_fields.intersection(
                    {"username", "password"}
                ):
                    try:
                        base_token = check_config.get("credentials")
                        base_credentials = (
                            decrypt_credentials(
                                base_token,
                                credential_key=app.config.get("CHECK_CREDENTIAL_KEY"),
                                secret_key=app.config.get("SECRET_KEY"),
                            )
                            if base_token
                            else ("", "")
                        )
                        override_credentials = decrypt_credentials(
                            override_token,
                            credential_key=app.config.get("CHECK_CREDENTIAL_KEY"),
                            secret_key=app.config.get("SECRET_KEY"),
                        )
                    except ValueError as error:
                        logger.exception(
                            "Scoring round %s could not decrypt credentials for service %s and team %s.",
                            round_id,
                            service["id"],
                            team.id,
                        )
                        score_round.status = "failed"
                        score_round.error_message = (
                            f"Scoring could not prepare team credentials: {str(error)[:350]}"
                        )
                        score_round.completed_at = datetime.now(timezone.utc)
                        db.session.commit()
                        return
                    check_config["credential_values"] = (
                        override_credentials[0]
                        if "username" in editable_fields
                        else base_credentials[0],
                        override_credentials[1]
                        if "password" in editable_fields
                        else base_credentials[1],
                    )
                host_address = hosts.get(
                    (team.id, service["host_group_id"])
                )
                if host_address or service["check_type"] == "dns":
                    jobs.append(
                        {
                            **service,
                            "check_config": check_config,
                            "team_id": team.id,
                            "team_name": team.name,
                            "address": host_address,
                        }
                    )
                else:
                    jobs.append(
                        {
                            **service,
                            "check_config": check_config,
                            "team_id": team.id,
                            "team_name": team.name,
                            "address": None,
                        }
                    )
        db.session.commit()
        if any(
            "credential_values" in job["check_config"]
            and not all(job["check_config"]["credential_values"])
            for job in jobs
        ):
            logger.warning(
                "Scoring round %s contains team-specific check credentials with an empty field.",
                round_id,
            )

    results = []
    try:
        with ThreadPoolExecutor(max_workers=app.config["SCORING_THREADS"]) as executor:
            futures = []
            for job in jobs:
                if job["address"] is None and job["check_type"] != "dns":
                    futures.append((job, None))
                else:
                    futures.append((job, executor.submit(_probe, job)))
            for job, future in futures:
                if future is None:
                    result = (False, "No host address is configured for this team.", None)
                else:
                    result = future.result()
                results.append((job, result))
    except Exception as error:
        logger.exception("Scoring round %s failed during execution.", round_id)
        with app.app_context():
            score_round = db.session.get(ScoreRound, round_id)
            if score_round:
                score_round.status = "failed"
                score_round.error_message = f"Scoring could not complete: {str(error)[:350]}"
                score_round.completed_at = datetime.now(timezone.utc)
                db.session.commit()
        return

    with app.app_context():
        score_round = db.session.get(ScoreRound, round_id)
        if score_round is None:
            logger.error("Scoring round %s was removed before results could be saved.", round_id)
            return

        for job, (passed, details, latency_ms) in results:
            points = (
                job["points"] + job.get("points_boost", 0)
                if passed
                else 0
            )
            result = ScoreResult(
                round_id=round_id,
                team_id=job["team_id"],
                service_id=job["id"],
                passed=passed,
                points=points,
                latency_ms=latency_ms,
                details=details[:500],
            )
            db.session.add(result)
            db.session.flush()
            credit = payout_per_point * points
            if credit > 0:
                db.session.add(
                    BankTransaction(
                        team_id=job["team_id"],
                        score_result_id=result.id,
                        kind="credit",
                        amount=credit,
                        description=f"Scoring round {score_round.number}: {job['name']}",
                    )
                )

        score_round.status = "completed"
        score_round.completed_at = datetime.now(timezone.utc)
        db.session.commit()
    logger.info("Completed scoring round %s with %s checks.", round_id, len(results))
