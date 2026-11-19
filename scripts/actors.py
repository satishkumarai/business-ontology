"""Actors: run SQL *as* a persona.

Two modes, chosen automatically (override with ONTOLOGY_ACTOR_MODE):

  users  Real separate Snowflake users with key-pair auth (CI / laptops).
         Full fidelity, including separation of duties across users.

  roles  One human user who, per action, holds exactly that persona's business
         role (all other persona roles revoked) with secondary roles disabled.
         Used where only one identity can authenticate (e.g. the Snowsight
         sandbox, whose proxy forces OAuth). Domain visibility and privilege
         checks are fully exercised; author/approver are the same user, so
         REQUIRE_DISTINCT_APPROVER must be FALSE for approvals in this mode.
"""

from __future__ import annotations

import json
import os

import personas


class Actor:
    def __init__(self, persona: str, harness: "Harness"):
        self.persona, self.h = persona, harness
        self.role = personas.PERSONAS[persona][0]

    def _cursor(self):
        return self.h.cursor_for(self)

    def query(self, sql: str, params=None) -> list[tuple]:
        return self._cursor().execute(sql, params or ()).fetchall()

    def call(self, sql: str, params=None):
        row = self._cursor().execute(sql, params or ()).fetchone()
        if row is None:
            return None
        v = row[0]
        return json.loads(v) if isinstance(v, str) and v[:1] in "{[" else v

    def put(self, local: str, stage_dir: str) -> None:
        self._cursor().execute(f"PUT 'file://{local}' {stage_dir} AUTO_COMPRESS=FALSE OVERWRITE=TRUE")


class Harness:
    def __init__(self, admin_conn, env: dict, mode: str | None = None):
        self.admin, self.env = admin_conn, env
        self.mode = mode or os.environ.get("ONTOLOGY_ACTOR_MODE") or self._detect()
        self._conns: dict[str, object] = {}
        self._active: str | None = None
        self.user = admin_conn.cursor().execute("SELECT CURRENT_USER()").fetchone()[0]

    def _detect(self) -> str:
        try:
            c = personas.connect_as("AUTHOR", self.env)
            c.cursor().execute("SELECT 1").fetchone()
            c.close()
            return "users"
        except Exception:
            return "roles"

    def actor(self, persona: str) -> Actor:
        return Actor(persona, self)

    def admin_cursor(self):
        cur = self.admin.cursor()
        cur.execute("USE ROLE ACCOUNTADMIN")
        cur.execute("USE SECONDARY ROLES ALL")
        self._active = None
        return cur

    def cursor_for(self, actor: Actor):
        if self.mode == "users":
            if actor.persona not in self._conns:
                self._conns[actor.persona] = personas.connect_as(actor.persona, self.env)
            return self._conns[actor.persona].cursor()
        cur = self.admin.cursor()
        if self._active != actor.persona:
            cur.execute("USE ROLE ACCOUNTADMIN")
            for p, (role, _) in personas.PERSONAS.items():
                verb = "GRANT ROLE {r} TO USER {u}" if p == actor.persona else "REVOKE ROLE {r} FROM USER {u}"
                cur.execute(verb.format(r=role, u=self.user))
            cur.execute(f"USE ROLE {actor.role}")
            cur.execute("USE SECONDARY ROLES NONE")
            cur.execute(f"USE WAREHOUSE {self.env['WAREHOUSE']}")
            self._active = actor.persona
        return cur

    def close(self) -> None:
        if self.mode == "roles":
            cur = self.admin.cursor()
            cur.execute("USE ROLE ACCOUNTADMIN")
            for role, _ in personas.PERSONAS.values():
                cur.execute(f"REVOKE ROLE {role} FROM USER {self.user}")
            cur.execute("USE SECONDARY ROLES ALL")
        for c in self._conns.values():
            c.close()
