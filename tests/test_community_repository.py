from __future__ import annotations

from yonerai_discord.modules.community import (
    CommunityRepository,
    Poll,
    PollStatus,
    Suggestion,
    SuggestionStatus,
    Ticket,
)


def test_tickets_are_guild_isolated_and_only_one_open_per_owner(tmp_path) -> None:
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        assert repository.create_ticket(Ticket("t1", 1, 10, "Guild 1"))
        assert not repository.create_ticket(Ticket("t2", 1, 10, "duplicate"))
        assert repository.create_ticket(Ticket("t3", 2, 10, "Guild 2"))
        assert repository.bind_ticket_channel(1, "t1", 100)
        assert repository.bind_ticket_channel(2, "t3", 200)
        assert repository.ticket_by_channel(1, 100) is not None
        assert repository.ticket_by_channel(2, 100) is None
        assert repository.close_ticket(1, "t1")
        assert not repository.close_ticket(1, "t1")
        assert repository.create_ticket(Ticket("t4", 1, 10, "new after close"))
    finally:
        repository.close()


def test_ticket_participant_and_state_transitions_respect_guild(tmp_path) -> None:
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        repository.create_ticket(Ticket("t1", 1, 10, "help"))
        assert not repository.add_ticket_participant(2, "t1", 20)
        assert repository.add_ticket_participant(1, "t1", 20)
        assert repository.ticket_participant_ids(1, "t1") == (20,)
        assert repository.ticket_participant_ids(2, "t1") == ()
        assert repository.has_ticket_participant(1, "t1", 20)
        assert not repository.has_ticket_participant(2, "t1", 20)
        assert not repository.add_ticket_participant(1, "t1", 20)
        assert repository.remove_ticket_participant(1, "t1", 20)
        assert not repository.has_ticket_participant(1, "t1", 20)
        repository.close_ticket(1, "t1")
        assert repository.ticket_participant_ids(1, "t1") == ()
        assert not repository.add_ticket_participant(1, "t1", 30)
    finally:
        repository.close()


def test_poll_rejects_duplicate_vote_and_cross_guild_access(tmp_path) -> None:
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        repository.create_poll(Poll("p1", 1, 10, "どちら？", ("A", "B")))
        assert repository.vote(1, "p1", 20, 0)
        assert not repository.vote(1, "p1", 20, 1)
        assert not repository.vote(2, "p1", 21, 0)
        assert [result.votes for result in repository.poll_results(1, "p1")] == [1, 0]
        assert repository.poll_results(2, "p1") == ()
        assert repository.close_poll(1, "p1")
        assert not repository.close_poll(1, "p1")
        assert not repository.vote(1, "p1", 21, 0)
        assert repository.get_poll(1, "p1").status is PollStatus.CLOSED  # type: ignore[union-attr]
    finally:
        repository.close()


def test_suggestions_and_selfroles_are_guild_isolated(tmp_path) -> None:
    repository = CommunityRepository(tmp_path / "community.sqlite3")
    repository.open()
    try:
        repository.create_suggestion(Suggestion("s1", 1, 10, "提案"))
        assert repository.get_suggestion(2, "s1") is None
        assert repository.update_suggestion(1, "s1", SuggestionStatus.ACCEPTED)
        assert repository.get_suggestion(1, "s1").status is SuggestionStatus.ACCEPTED  # type: ignore[union-attr]

        assert repository.add_selfrole(1, 100, 10)
        assert not repository.add_selfrole(1, 100, 10)
        assert repository.add_selfrole(2, 100, 10)
        assert repository.selfroles(1) == (100,)
        assert repository.remove_selfrole(1, 100)
        assert repository.selfroles(1) == ()
        assert repository.selfroles(2) == (100,)
    finally:
        repository.close()
