    def append_platform_delivery(self, session_id, content, metadata):
        """Commit an acknowledged external delivery once under native write guards."""
        if not metadata.get("delivery_id"):
            raise ValueError("Delivery identity required")
        msg = {"content": content, "platform_message_id": metadata["message_id"],
               "display_kind": "platform_delivery", "display_metadata": metadata}
        params = self._message_row_params(session_id, "user", msg, None, time.time(), keep_reasoning=True)

        def _do(conn):
            row = conn.execute("SELECT source,chat_id,thread_id,user_id,ended_at FROM sessions WHERE id=?", (session_id,)).fetchone()
            expected = tuple(metadata[k] for k in ("platform", "chat_id", "thread_id", "user_id"))
            if row is None or tuple(str(row[k] or "") for k in range(4)) != expected or row[4] is not None:
                raise ValueError("Delivery conversation changed")
            if metadata.get("participant_inferred"):
                owners = conn.execute(
                    "SELECT DISTINCT COALESCE(user_id,'') FROM sessions WHERE LOWER(source)=LOWER(?) "
                    "AND chat_id=? AND COALESCE(thread_id,'')=? AND session_key IS NOT NULL AND ended_at IS NULL",
                    expected[:3]).fetchall()
                if len(owners) != 1 or str(owners[0][0]) != expected[3]:
                    raise ValueError("Delivery conversation owner became ambiguous")
            existing = conn.execute(
                "WITH RECURSIVE lineage(id) AS (SELECT ? UNION "
                "SELECT s.parent_session_id FROM sessions s JOIN lineage l ON s.id=l.id "
                "JOIN sessions p ON p.id=s.parent_session_id WHERE p.end_reason='compression') "
                "SELECT m.id,m.content FROM messages m JOIN lineage l ON m.session_id=l.id "
                "WHERE m.display_kind='platform_delivery' "
                "AND json_extract(m.display_metadata,'$.delivery_id')=? LIMIT 1",
                (session_id, metadata["delivery_id"])).fetchone()
            if existing is not None:
                if existing["content"] != content:
                    raise ValueError("Delivery identity has conflicting transcript content")
                return existing[0]
            self._check_transcript_write_guards(conn, session_id, None,
                reject_active_turn_lease=True, reject_active_compression_lock=True)
            previous = conn.execute(
                "SELECT id,json_extract(display_metadata,'$.revision') AS revision FROM messages "
                "WHERE session_id=? AND display_kind='platform_delivery' AND platform_message_id=? "
                "ORDER BY revision DESC LIMIT 1", (session_id, metadata["message_id"])).fetchone()
            if previous is not None and previous["revision"] >= metadata["revision"]:
                return previous["id"]
            # Keep the audit row, but ordinary follow-up history sees the latest edit only.
            conn.execute("UPDATE messages SET active=0,compacted=0 WHERE session_id=? "
                         "AND display_kind='platform_delivery' AND platform_message_id=?",
                         (session_id, metadata["message_id"]))
            message_id = conn.execute(_INSERT_MESSAGE_SQL, params).lastrowid
            self._bump_session_counters(conn, session_id, 1, 0, unit=True)
            return message_id

        return self._execute_write(_do, patience_s=self._TRANSCRIPT_WRITE_PATIENCE_S)

