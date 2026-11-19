"""Demo personas: real, separate Snowflake users so separation of duties and
row-level visibility are exercised end to end (not simulated with one user).

Users are TYPE=SERVICE with key-pair auth; private keys are generated fresh on
every run and kept outside the repo (~/.ontology_demo). Requires a role that can
CREATE USER / CREATE ROLE (ACCOUNTADMIN or USERADMIN+SECURITYADMIN).
"""

from __future__ import annotations

import os
import pathlib

KEY_DIR = pathlib.Path(os.environ.get("ONTOLOGY_DEMO_KEY_DIR", pathlib.Path.home() / ".ontology_demo"))

# persona -> (business role, {domain: privilege})
PERSONAS = {
    "AUTHOR":        ("ONT_DEMO_AUTHOR_ROLE",        {"Finance": "SUGGEST", "Sales": "SUGGEST", "Finance.EMEA": "SUGGEST"}),
    "FIN_STEWARD":   ("ONT_DEMO_FIN_STEWARD_ROLE",   {"Finance": "MODIFY", "Finance.EMEA": "MODIFY"}),
    "SALES_STEWARD": ("ONT_DEMO_SALES_STEWARD_ROLE", {"Sales": "MODIFY"}),
    "FIN_ANALYST":   ("ONT_DEMO_FIN_ANALYST_ROLE",   {"Finance": "READ"}),
    "EXEC":          ("ONT_DEMO_EXEC_ROLE",          {"Finance": "READ", "Sales": "READ"}),
    "OUTSIDER":      ("ONT_DEMO_OUTSIDER_ROLE",      {}),
}

DOMAINS = [
    ("Finance", "Financial reporting definitions (recognized revenue)", "ONT_DEMO_FIN_STEWARD_ROLE"),
    ("Sales", "Sales operations definitions (bookings)", "ONT_DEMO_SALES_STEWARD_ROLE"),
    ("Finance.EMEA", "EMEA statutory variants of Finance definitions", "ONT_DEMO_FIN_STEWARD_ROLE"),
]


def user_name(persona: str) -> str:
    return f"ONT_DEMO_{persona}"


def _keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    pub_body = "".join(pub.decode().strip().splitlines()[1:-1])
    return pem, pub_body


def ensure_personas(conn, env: dict, create_users: bool = True) -> None:
    """Create business roles (+ key-pair users), wire warehouse/consumer access. Idempotent."""
    cur = conn.cursor()
    cur.execute("USE ROLE ACCOUNTADMIN")
    KEY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    for persona, (role, _) in PERSONAS.items():
        user = user_name(persona)
        cur.execute(f"CREATE ROLE IF NOT EXISTS {role} COMMENT = 'Business Ontology demo persona role'")
        cur.execute(f"GRANT USAGE ON WAREHOUSE {env['WAREHOUSE']} TO ROLE {role}")
        # Outsider gets platform access only (no domain privileges) to prove "does not exist" semantics.
        cur.execute(f"GRANT ROLE {env['CONSUMER_ROLE']} TO ROLE {role}")
        if not create_users:
            continue
        cur.execute(
            f"CREATE USER IF NOT EXISTS {user} TYPE = SERVICE DEFAULT_ROLE = {role} "
            f"DEFAULT_WAREHOUSE = {env['WAREHOUSE']} COMMENT = 'Business Ontology demo persona'"
        )
        pem, pub = _keypair()
        cur.execute(f"ALTER USER {user} SET RSA_PUBLIC_KEY = '{pub}'")
        path = KEY_DIR / f"{user}.p8"
        path.write_bytes(pem)
        path.chmod(0o600)
        cur.execute(f"GRANT ROLE {role} TO USER {user}")


def connect_as(persona: str, env: dict):
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization

    user = user_name(persona)
    key = serialization.load_pem_private_key((KEY_DIR / f"{user}.p8").read_bytes(), password=None)
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    acct = os.environ.get("SNOWFLAKE_ACCOUNT")
    return snowflake.connector.connect(
        account=acct,
        host=os.environ.get("SNOWFLAKE_HOST"),
        user=user,
        private_key=der,
        role=PERSONAS[persona][0],
        warehouse=env["WAREHOUSE"],
        database=env["DB"],
        session_parameters={"QUERY_TAG": f"ontology-demo:{persona}"},
    )


def drop_personas(conn) -> None:
    cur = conn.cursor()
    cur.execute("USE ROLE ACCOUNTADMIN")
    for persona, (role, _) in PERSONAS.items():
        cur.execute(f"DROP USER IF EXISTS {user_name(persona)}")
        cur.execute(f"DROP ROLE IF EXISTS {role}")
