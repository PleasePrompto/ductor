"""Central message bus: intake, lock, inject, deliver."""

from __future__ import annotations

import logging
import secrets
from typing import Protocol, runtime_checkable

from ductor_bot.bus.envelope import DeliveryMode, Envelope, LockMode
from ductor_bot.bus.lock_pool import LockPool

logger = logging.getLogger(__name__)


@runtime_checkable
class TransportAdapter(Protocol):
    """Protocol for transport-specific delivery."""

    @property
    def transport_name(self) -> str:
        """Short identifier for this transport (e.g. ``"tg"``, ``"mx"``)."""
        ...

    async def deliver(self, envelope: Envelope) -> None:
        """Send the envelope's result to the targeted chat."""
        ...

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        """Send the envelope's result to all allowed users."""
        ...


@runtime_checkable
class SessionInjector(Protocol):
    """Protocol for injecting a prompt into the active CLI session.

    Implemented by the Orchestrator.  The bus calls this when
    ``envelope.needs_injection`` is True.
    """

    async def inject_prompt(
        self,
        prompt: str,
        chat_id: int,
        label: str,
        *,
        topic_id: int | None = None,
        transport: str = "tg",
    ) -> str:
        """Execute *prompt* in the active session. Returns response text."""
        ...


class MessageBus:
    """Central coordinator for all background message routing.

    Usage::

        bus = MessageBus()
        bus.register_transport(TelegramTransport(bot))
        bus.set_injector(orchestrator)

        # Any observer callback:
        await bus.submit(from_cron_result(title, text, status))
    """

    def __init__(self, lock_pool: LockPool | None = None) -> None:
        self._locks = lock_pool if lock_pool is not None else LockPool()
        self._transports: list[TransportAdapter] = []
        self._injector: SessionInjector | None = None

    @property
    def lock_pool(self) -> LockPool:
        """The shared lock pool."""
        return self._locks

    def register_transport(self, transport: TransportAdapter) -> None:
        """Add a transport adapter for delivery."""
        self._transports.append(transport)

    def set_injector(self, injector: SessionInjector) -> None:
        """Set the session injector (typically the Orchestrator)."""
        self._injector = injector

    async def submit(self, envelope: Envelope) -> None:
        """Route an envelope: assign ID, acquire lock, inject, deliver."""
        if not envelope.envelope_id:
            envelope.envelope_id = secrets.token_hex(6)

        logger.debug(
            "Bus submit: origin=%s chat=%d delivery=%s lock=%s inject=%s",
            envelope.origin.value,
            envelope.chat_id,
            envelope.delivery.value,
            envelope.lock_mode.value,
            envelope.needs_injection,
        )

        if envelope.lock_mode == LockMode.REQUIRED:
            lock = self._locks.get(envelope.lock_key)
            async with lock:
                await self._process(envelope)
        else:
            await self._process(envelope)

    async def _process(self, envelope: Envelope) -> None:
        """Inject if needed, then deliver."""
        if envelope.needs_injection and self._injector and envelope.prompt:
            label = f"{envelope.origin.value}:{envelope.envelope_id}"
            try:
                response = await self._injector.inject_prompt(
                    envelope.prompt,
                    envelope.chat_id,
                    label,
                    topic_id=envelope.topic_id,
                    transport=envelope.transport,
                )
                envelope.result_text = response
            except Exception:
                logger.exception(
                    "Injection failed: origin=%s chat=%d",
                    envelope.origin.value,
                    envelope.chat_id,
                )
                envelope.is_error = True
                if not envelope.result_text:
                    envelope.result_text = f"Error processing {envelope.origin.value} result"

        await self._deliver(envelope)

    async def _deliver(self, envelope: Envelope) -> None:
        """Route to the correct transport(s) with cascading fallback."""
        if not self._transports:
            logger.warning(
                "No transports registered — envelope lost: origin=%s chat=%d",
                envelope.origin.value,
                envelope.chat_id,
            )
            envelope.delivered = False
            envelope.delivery_error = "no transports registered"
            return

        # BROADCAST goes to all transports unconditionally.
        if envelope.delivery == DeliveryMode.BROADCAST:
            for transport in self._transports:
                try:
                    await transport.deliver_broadcast(envelope)
                except Exception:
                    logger.exception(
                        "Broadcast delivery failed: %s",
                        type(transport).__name__,
                    )
            return

        # UNICAST: filter by transport name.
        target_transport = envelope.transport
        if target_transport:
            matching = [t for t in self._transports if t.transport_name == target_transport]
            others = [t for t in self._transports if t.transport_name != target_transport]
        else:
            # No transport specified -> deliver to all (backward compat).
            matching = list(self._transports)
            others = []

        for transport in matching:
            try:
                await transport.deliver(envelope)
            except Exception as exc:
                logger.exception(
                    "Transport delivery failed: origin=%s transport=%s",
                    envelope.origin.value,
                    type(transport).__name__,
                )
                envelope.delivered = False
                envelope.delivery_error = type(exc).__name__

        # Cascading fallback: target transport not registered at all.
        if not matching and others:
            envelope.delivered = False
            envelope.delivery_error = f"transport '{target_transport}' unavailable"
            logger.warning(
                "Transport '%s' not available for envelope origin=%s, falling back to %s",
                target_transport,
                envelope.origin.value,
                others[0].transport_name,
            )
            fallback_env = Envelope(
                origin=envelope.origin,
                chat_id=0,
                result_text=(
                    f"**Delivery fallback**\n\n"
                    f"Target transport '{target_transport}' is not available.\n\n"
                    f"---\n{envelope.result_text or ''}"
                ),
                status=envelope.status,
                delivery=DeliveryMode.BROADCAST,
                lock_mode=envelope.lock_mode,
                metadata=envelope.metadata,
            )
            try:
                await others[0].deliver_broadcast(fallback_env)
            except Exception:
                logger.exception("Fallback delivery also failed")
