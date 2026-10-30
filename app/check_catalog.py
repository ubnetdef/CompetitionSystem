TEAM_CREDENTIAL_CHECK_TYPES = frozenset({"ssh", "winrm", "ldap", "sql", "ftp"})
TEAM_EDITABLE_CREDENTIAL_FIELDS = frozenset({"username", "password"})


CHECK_CATALOG = {
    "http": {
        "label": "HTTP / HTTPS",
        "available": True,
        "description": "Check one configured web response.",
        "properties": ("Scheme", "Subdomain", "Path", "Port", "ExpectedStatus", "ExpectedOutput"),
    },
    "dns": {
        "label": "DNS lookup",
        "available": True,
        "description": "Resolve one configured DNS record and compare its answer.",
        "properties": ("Lookup", "RecordType", "ExpectedOutput"),
    },
    "ping": {
        "label": "PING",
        "available": True,
        "description": "Send a bounded number of ICMP echo requests to the team host.",
        "properties": ("Protocol", "Attempts"),
    },
    "ssh": {
        "label": "SSH authentication",
        "available": True,
        "description": "Verify SSH authentication and a pinned host key; no remote command is run.",
        "properties": ("Port", "Username", "Password", "HostKeyFingerprint"),
    },
    "ldap": {
        "label": "LDAP search",
        "available": True,
        "description": "Run a bounded read-only LDAP search over verified TLS.",
        "properties": (
            "ApplicationProtocol",
            "TransportProtocol",
            "Domain",
            "BaseDN",
            "Filter",
            "Attributes",
            "Port",
            "Username",
            "Password",
        ),
    },
    "sql": {
        "label": "MySQL check",
        "available": True,
        "description": "Verify MySQL authentication over TLS using a fixed read-only SELECT 1 query.",
        "properties": ("DBType", "DBName", "Port", "Username", "Password", "Command", "MinExpectedRows", "MaxExpectedRows"),
    },
    "ftp": {
        "label": "FTPS read check",
        "available": True,
        "description": "Read a configured file over verified TLS and check its contents; never writes.",
        "properties": ("Port", "Username", "Password", "ReadFilename", "WriteFilename", "Text", "ExpectedOutput"),
    },
    "winrm": {
        "label": "WinRM authentication",
        "available": True,
        "description": "Verify HTTPS, certificate trust, and credentials by opening and immediately closing a remote shell; no command is run.",
        "properties": ("Scheme", "ClientType", "Port", "Username", "Password"),
    },
    "smb": {
        "label": "SMB",
        "available": False,
        "description": "Legacy engine check type; not implemented in this platform.",
        "properties": (),
    },
    "imap": {
        "label": "IMAP",
        "available": False,
        "description": "Legacy engine check type; not implemented in this platform.",
        "properties": (),
    },
    "caldav": {
        "label": "CalDav",
        "available": False,
        "description": "Legacy engine check type; not implemented in this platform.",
        "properties": (),
    },
}

LEGACY_SERVICE_PROPERTIES = ("weight", "pointsBoost", "roundUnits", "roundDelay")
