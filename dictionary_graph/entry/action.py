from functools import wraps

from django.core.exceptions import PermissionDenied
from django.db.models import F
from django.urls import reverse_lazy
from django.utils.translation import gettext as _

from graphene import ID, Int, Mutation, String

from dictionary.conf import settings
from dictionary.models import Entry, Comment

from dictionary_graph.utils import AnonymousUserStorage, login_required

# pylint: disable=too-many-arguments


def owneraction(mutator):
    """Checks if sender is actually the owner of the object & gets the Entry object."""

    @wraps(mutator)
    @login_required
    def decorator(_root, info, pk):
        entry, sender = Entry.objects_all.get(pk=pk), info.context.user
        if entry.author != sender:
            raise PermissionDenied(_("we couldn't handle your request. try again later."))
        return mutator(_root, info, entry)

    return decorator


class Action:
    """Meta class for entry action mutations."""

    class Arguments:
        pk = ID()

    feedback = String()


class DeleteEntry(Action, Mutation):
    redirect = String()

    @staticmethod
    @owneraction
    def mutate(_root, info, entry):
        entry.delete()
        redirect_url = reverse_lazy("topic", kwargs={"slug": entry.topic.slug})

        if not entry.is_draft:
            # Deduct some karma upon entry deletion.
            entry.author.karma = F("karma") - 1
            entry.author.save(update_fields=["karma"])

        return DeleteEntry(feedback=_("your entry has been deleted"), redirect=redirect_url)


class PinEntry(Action, Mutation):
    @staticmethod
    @owneraction
    def mutate(_root, info, entry):
        feedback = _("this entry is now pinned")
        current = info.context.user.pinned_entry

        if current == entry:
            info.context.user.pinned_entry = None
            feedback = _("this entry is no longer pinned")
        else:
            info.context.user.pinned_entry = entry

        info.context.user.save()
        return PinEntry(feedback=feedback)


class FavoriteEntry(Action, Mutation):
    count = Int()

    @staticmethod
    @login_required
    def mutate(_root, info, pk):
        entry = Entry.objects_published.get(pk=pk)

        if entry.author.blocked.filter(pk=info.context.user.pk).exists():
            raise PermissionDenied(_("we couldn't handle your request. try again later."))

        if info.context.user.favorite_entries.filter(pk=pk).exists():
            info.context.user.favorite_entries.remove(entry)
            return FavoriteEntry(
                feedback=_("the entry has been removed from favorites"), count=entry.favorited_by.count()
            )

        info.context.user.favorite_entries.add(entry)
        return FavoriteEntry(feedback=_("the entry has been favorited"), count=entry.favorited_by.count())


def voteaction(mutator):
    """Checks if sender is actually the owner of the object, gets the Entry object. Handle anonymous votes."""

    @wraps(mutator)
    def decorator(_root, info, pk):
        entry, sender = Entry.objects_all.get(pk=pk), info.context.user

        if entry.author == sender:
            raise PermissionDenied(_("we couldn't handle your request. try again later."))

        if not sender.is_authenticated:
            if settings.DISABLE_ANONYMOUS_VOTING:
                raise PermissionDenied(_("we couldn't handle your request. try again later."))

            sender = AnonymousUserStorage(info.context)

        upvoted, downvoted = sender.upvoted_entries, sender.downvoted_entries
        in_upvoted, in_downvoted = upvoted.filter(pk=pk).exists(), downvoted.filter(pk=pk).exists()
        exceeded, reason = sender.has_exceeded_vote_limit(against=entry.author)

        constants = (
            F("karma"),
            settings.KARMA_RATES["cost"],
            settings.KARMA_RATES["downvote"],
            settings.KARMA_RATES["upvote"],
        )

        return mutator(_root, entry, sender, upvoted, downvoted, in_upvoted, in_downvoted, constants, exceeded, reason)

    return decorator


class UpvoteEntry(Action, Mutation):
    """Mutation to upvote an entry."""

    @staticmethod
    @voteaction
    def mutate(_root, entry, sender, upvoted, downvoted, in_upvoted, in_downvoted, constants, exceeded, reason):
        response = UpvoteEntry(feedback=None)
        karma, cost, downvote_rate, upvote_rate = constants

        # User removes the upvote
        if in_upvoted:
            upvoted.remove(entry)

            if sender.is_karma_eligible:
                sender.karma = karma + cost  # refund
                entry.author.karma = karma - upvote_rate
                sender.save()
                entry.author.save()

            return response

        # User changes from downvote to upvote
        if in_downvoted:
            downvoted.remove(entry)
            upvoted.add(entry)

            if sender.is_karma_eligible:
                entry.author.karma = karma + (downvote_rate + upvote_rate)
                entry.author.save()

            return response

        if exceeded:
            return UpvoteEntry(feedback=reason)

        # Usual upvote
        upvoted.add(entry)

        if sender.is_karma_eligible:
            sender.karma = karma - cost
            entry.author.karma = karma + upvote_rate
            sender.save()
            entry.author.save()

        return response


class DownvoteEntry(Action, Mutation):
    """Mutation to downvote an entry, same logic with UpvoteEntry but reversed."""

    @staticmethod
    @voteaction
    def mutate(_root, entry, sender, upvoted, downvoted, in_upvoted, in_downvoted, constants, exceeded, reason):
        response = DownvoteEntry(feedback=None)
        karma, cost, downvote_rate, upvote_rate = constants

        # User removes the downvote
        if in_downvoted:
            downvoted.remove(entry)

            if sender.is_karma_eligible:
                sender.karma = karma + cost  # refund
                entry.author.karma = karma + downvote_rate
                sender.save()
                entry.author.save()
            return response

        # User changes from upvote to downvote
        if in_upvoted:
            upvoted.remove(entry)
            downvoted.add(entry)

            if sender.is_karma_eligible:
                entry.author.karma = karma - (downvote_rate + upvote_rate)
                entry.author.save()

            return response

        if exceeded:
            return DownvoteEntry(feedback=reason)

        # Usual downvote
        downvoted.add(entry)

        if sender.is_karma_eligible:
            sender.karma = karma - cost
            entry.author.karma = karma - downvote_rate
            sender.save()
            entry.author.save()

        return response


class VoteComment(Mutation):
    count = Int()

    class Arguments:
        pk = ID()
        action = String()

    @staticmethod
    @login_required
    def mutate(_root, info, pk, action):
        comment, sender = Comment.objects.get(pk=pk), info.context.user
        in_upvoted = comment.upvoted_by.filter(pk=sender.pk).exists()
        in_downvoted = comment.downvoted_by.filter(pk=sender.pk).exists()

        if action == "upvote":
            if in_upvoted:
                comment.upvoted_by.remove(sender)
            elif in_downvoted:
                comment.downvoted_by.remove(sender)
                comment.upvoted_by.add(sender)
            else:
                comment.upvoted_by.add(sender)
        elif action == "downvote":
            if in_downvoted:
                comment.downvoted_by.remove(sender)
            elif in_upvoted:
                comment.upvoted_by.remove(sender)
                comment.downvoted_by.add(sender)
            else:
                comment.downvoted_by.add(sender)
        else:
            raise ValueError("That action is not available")

        count = comment.upvoted_by.count() - comment.downvoted_by.count()
        return VoteComment(count=count)
