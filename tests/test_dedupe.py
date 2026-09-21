"""One transcript under two Graph ids must stay one transcript."""

import json

from app.store import artifact_aliases, dedupe_content, dedupe_items
from scripts.dedupe_meetings import apply, scan, snapshot
from tests.conftest import USER

NOTE = {"title": "Budget", "text": "Agreed the date"}
OTHER = {"title": "Timeline", "text": "Ship in March"}


class TestRepetitionInsideOneSummary:
    """A model listing the same point several times in a single reply."""

    def test_exact_repeats_collapse_and_order_is_kept(self):
        assert dedupe_items([NOTE, OTHER, NOTE]) == [NOTE, OTHER]

    def test_distinct_points_all_survive(self):
        assert dedupe_items([NOTE, OTHER]) == [NOTE, OTHER]

    def test_key_order_does_not_make_two_copies_look_different(self):
        reordered = {"text": NOTE["text"], "title": NOTE["title"]}
        assert dedupe_items([NOTE, reordered]) == [NOTE]

    def test_points_differing_in_any_field_are_both_kept(self):
        nearly = {**NOTE, "text": NOTE["text"] + "."}
        assert dedupe_items([NOTE, nearly]) == [NOTE, nearly]

    def test_nested_subpoints_are_part_of_the_comparison(self):
        with_sub = {**NOTE, "subpoints": [{"title": "a", "text": "b"}]}
        assert dedupe_items([NOTE, with_sub]) == [NOTE, with_sub]
        assert dedupe_items([with_sub, with_sub]) == [with_sub]

    def test_an_empty_list_is_unchanged(self):
        assert dedupe_items([]) == []

    def test_repeated_notes_are_cleaned_and_reported(self):
        content = {"insights": [insight("i", notes=[NOTE, NOTE, OTHER])]}
        cleaned, report = dedupe_content(content)
        assert cleaned["insights"][0]["insight"]["meetingNotes"] == [NOTE, OTHER]
        assert report == {"meetingNotes": {"before": 3, "after": 2}}

    def test_action_items_are_cleaned_too(self):
        task = {"title": "Send proposal", "ownerDisplayName": "Ada"}
        content = {"insights": [{"insight": {"id": "i", "actionItems": [task, task]}}]}
        cleaned, report = dedupe_content(content)
        assert cleaned["insights"][0]["insight"]["actionItems"] == [task]
        assert report == {"actionItems": {"before": 2, "after": 1}}

    def test_counts_accumulate_across_both_providers(self):
        content = {
            "insights": [
                insight("c1", "copilot", [NOTE, NOTE]),
                insight("openrouter:m", "openrouter", [OTHER, OTHER, OTHER]),
            ]
        }
        cleaned, report = dedupe_content(content)
        # Two copies of NOTE collapse to one, three of OTHER to one: 5 -> 2.
        assert report == {"meetingNotes": {"before": 5, "after": 2}}
        assert [len(i["insight"]["meetingNotes"]) for i in cleaned["insights"]] == [1, 1]

    def test_a_clean_summary_reports_nothing_and_is_untouched(self):
        content = {"insights": [insight("i", notes=[NOTE, OTHER])]}
        cleaned, report = dedupe_content(content)
        assert report == {}
        assert cleaned == content

    def test_the_caller_s_own_dict_is_never_mutated(self):
        """dedupe_content is pure; save_meeting still holds these dicts."""
        original = insight("i", notes=[NOTE, NOTE])
        content = {"insights": [original]}
        dedupe_content(content)
        assert original["insight"]["meetingNotes"] == [NOTE, NOTE]

    def test_transcripts_are_left_alone(self):
        content = {"transcripts": [transcript("a", "a")]}
        cleaned, report = dedupe_content(content)
        assert report == {} and cleaned == content

    def test_save_meeting_strips_repeats_on_the_way_in(self, store):
        store.save_meeting(
            USER,
            "Standup",
            {"meeting_id": "m", "insight": {"id": "i", "meetingNotes": [NOTE, NOTE]}, "card": {}},
        )
        saved = store.meetings(USER)[0]["content"]["insights"][0]["insight"]
        assert saved["meetingNotes"] == [NOTE]


def transcript(id_, source_id=None, **extra):
    body = {"id": id_, "local_id": 1, **extra}
    if source_id:
        body["source_id"] = source_id
    return {"transcript": body}


def insight(id_, provider="copilot", notes=None, **extra):
    return {
        "insight": {"id": id_, "provider": provider, "meetingNotes": notes or [], **extra},
        "card": {},
    }


class TestAliases:
    def test_both_graph_names_identify_the_artifact(self):
        assert artifact_aliases({"id": "A", "source_id": "B"}) == {"A", "B"}

    def test_missing_and_empty_ids_are_not_aliases(self):
        # A shared None would make every entry match every other entry.
        assert artifact_aliases({"id": "A", "source_id": None}) == {"A"}
        assert artifact_aliases({"id": "A", "source_id": ""}) == {"A"}
        assert artifact_aliases({}) == set()

    def test_insights_are_also_fingerprinted_by_content(self):
        """OpenRouter keys on the transcript id, so aliased transcripts share no id."""
        one = artifact_aliases({"id": "openrouter:A", "meetingNotes": ["same"]})
        two = artifact_aliases({"id": "openrouter:B", "meetingNotes": ["same"]})
        assert one & two
        assert not one & artifact_aliases({"id": "openrouter:C", "meetingNotes": ["other"]})

    def test_an_empty_insight_is_not_fingerprinted(self):
        """Two insights still waiting for content must not collapse into one."""
        assert not artifact_aliases({"id": "x"}) & artifact_aliases({"id": "y"})


class TestDedupeContent:
    def test_one_transcript_under_two_ids_collapses(self):
        content = {"transcripts": [transcript("A", "B"), transcript("B", "B")]}
        cleaned, report = dedupe_content(content)
        assert len(cleaned["transcripts"]) == 1
        assert report == {"transcripts": {"before": 2, "after": 1}}

    def test_identical_openrouter_summaries_collapse(self):
        content = {
            "insights": [
                insight("openrouter:A", "openrouter", ["Agreed the date"]),
                insight("openrouter:B", "openrouter", ["Agreed the date"]),
            ]
        }
        cleaned, report = dedupe_content(content)
        assert len(cleaned["insights"]) == 1
        assert report == {"insights": {"before": 2, "after": 1}}

    def test_two_genuine_transcripts_are_both_kept(self):
        """A meeting whose transcription was stopped and restarted has two."""
        content = {
            "transcripts": [transcript("A", "A"), transcript("B", "B")],
            "insights": [
                insight("i1", notes=["first half"]),
                insight("i2", notes=["second half"]),
            ],
        }
        cleaned, report = dedupe_content(content)
        assert report == {}
        assert len(cleaned["transcripts"]) == 2
        assert len(cleaned["insights"]) == 2

    def test_copilot_and_openrouter_insights_are_never_merged(self):
        content = {
            "insights": [
                insight("c1", "copilot", ["Agreed the date"]),
                insight("openrouter:A", "openrouter", ["Agreed the date"]),
            ]
        }
        # Identical text from two providers is the side-by-side comparison the
        # product exists to show, not a duplicate.
        cleaned, report = dedupe_content(content)
        assert report == {}
        assert len(cleaned["insights"]) == 2

    def test_the_later_copy_wins_because_copilot_revises_in_place(self):
        content = {
            "insights": [
                insight("A", notes=["draft"], source_id="A"),
                insight("A", notes=["revised"], source_id="A"),
            ]
        }
        cleaned, _ = dedupe_content(content)
        assert cleaned["insights"][0]["insight"]["meetingNotes"] == ["revised"]

    def test_surviving_order_is_preserved(self):
        content = {
            "transcripts": [transcript("A"), transcript("B"), transcript("A", "A"), transcript("C")]
        }
        cleaned, _ = dedupe_content(content)
        assert [x["transcript"]["id"] for x in cleaned["transcripts"]] == ["A", "B", "C"]

    def test_singular_mirror_points_at_a_survivor(self):
        content = {
            "transcript": {"id": "B", "source_id": "B"},
            "transcripts": [transcript("A", "B"), transcript("B", "B")],
        }
        cleaned, _ = dedupe_content(content)
        ids = {x["transcript"]["id"] for x in cleaned["transcripts"]}
        assert cleaned["transcript"]["id"] in ids

    def test_a_clean_meeting_is_returned_untouched_and_the_pass_is_idempotent(self):
        content = {
            "meeting_id": "m",
            "transcripts": [transcript("A", "A")],
            "insights": [insight("i", notes=["note"])],
        }
        cleaned, report = dedupe_content(content)
        assert report == {}
        assert cleaned == content
        assert dedupe_content(cleaned)[0] == cleaned

    def test_a_meeting_with_no_artifacts_survives(self):
        cleaned, report = dedupe_content({"meeting_id": "m"})
        assert report == {}
        assert cleaned == {"meeting_id": "m"}


class TestSaveMeetingGuard:
    def test_the_second_id_does_not_create_a_second_entry(self, store):
        """The production case: sync_meeting finds the transcript under its other name."""
        store.save_meeting(USER, "Standup", {"meeting_id": "m", "transcript": {"id": "A"}})
        store.save_meeting(
            USER, "Standup", {"meeting_id": "m", "transcript": {"id": "B", "source_id": "A"}}
        )
        saved = store.meetings(USER)[0]["content"]
        assert len(saved["transcripts"]) == 1

    def test_a_second_genuine_transcript_is_still_recorded(self, store):
        store.save_meeting(USER, "Standup", {"meeting_id": "m", "transcript": {"id": "A"}})
        store.save_meeting(USER, "Standup", {"meeting_id": "m", "transcript": {"id": "B"}})
        assert len(store.meetings(USER)[0]["content"]["transcripts"]) == 2

    def test_an_existing_duplicate_heals_on_the_next_write(self, store):
        """Rows the worker still touches repair themselves; the script is for the rest."""
        store.save_meeting(USER, "Standup", {"meeting_id": "m"})
        with store.connect() as db:
            db.execute(
                "UPDATE meetings SET content=? WHERE user_id=?",
                (
                    json.dumps(
                        {"meeting_id": "m", "transcripts": [transcript("A", "B"), transcript("B")]}
                    ),
                    USER,
                ),
            )
        store.save_meeting(USER, "Standup", {"meeting_id": "m", "insight": {"id": "i"}, "card": {}})
        assert len(store.meetings(USER)[0]["content"]["transcripts"]) == 1


class TestCleanupScript:
    def duplicated(self, store):
        store.save_meeting(USER, "Standup", {"meeting_id": "m"})
        with store.connect() as db:
            db.execute(
                "UPDATE meetings SET content=? WHERE user_id=?",
                (
                    json.dumps(
                        {
                            "meeting_id": "m",
                            "transcripts": [transcript("A", "B"), transcript("B", "B")],
                            "insights": [
                                insight("openrouter:A", "openrouter", ["dup"]),
                                insight("openrouter:B", "openrouter", ["dup"]),
                            ],
                        }
                    ),
                    USER,
                ),
            )

    def test_plan_reports_without_writing(self, store):
        self.duplicated(store)
        work, scanned = scan(store)
        assert len(work) == 1
        assert work[0]["report"] == {
            "transcripts": {"before": 2, "after": 1},
            "insights": {"before": 2, "after": 1},
        }
        # Report-only: the row must be exactly as it was.
        assert len(store.meetings(USER)[0]["content"]["transcripts"]) == 2

    def test_apply_writes_and_a_second_pass_finds_nothing(self, store):
        self.duplicated(store)
        written, skipped = apply(store, scan(store)[0])
        assert (written, skipped) == (1, [])
        saved = store.meetings(USER)[0]["content"]
        assert len(saved["transcripts"]) == 1
        assert len(saved["insights"]) == 1
        assert scan(store)[0] == []

    def test_a_row_changed_underneath_is_skipped_not_clobbered(self, store):
        """The worker may write between the read and the update."""
        self.duplicated(store)
        work, scanned = scan(store)
        store.save_meeting(
            USER, "Standup", {"meeting_id": "m", "insight": {"id": "late"}, "card": {}}
        )
        written, skipped = apply(store, work)
        assert written == 0 and skipped
        # The worker's insight survived rather than being overwritten by stale text.
        ids = {x["insight"]["id"] for x in store.meetings(USER)[0]["content"]["insights"]}
        assert "late" in ids

    def test_clean_database_reports_no_work(self, store):
        store.save_meeting(USER, "Standup", {"meeting_id": "m", "transcript": {"id": "A"}})
        assert scan(store)[0] == []

    def test_the_snapshot_holds_the_rows_as_they_were_before_the_write(self, store):
        """The undo path: a copy beside the original, not a server restore."""
        self.duplicated(store)
        work, scanned = scan(store)
        table = snapshot(store)
        apply(store, work)
        with store.connect() as db:
            saved = json.loads(
                db.execute(f"SELECT content FROM {table} WHERE user_id=?", (USER,)).fetchone()[0]
            )
        assert len(saved["transcripts"]) == 2
        assert len(store.meetings(USER)[0]["content"]["transcripts"]) == 1

    def test_each_snapshot_gets_its_own_table(self, store):
        store.save_meeting(USER, "Standup", {"meeting_id": "m"})
        assert snapshot(store) != snapshot(store)
