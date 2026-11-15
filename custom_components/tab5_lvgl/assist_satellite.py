"""Assist satellite entity for the microphone built into a HomeTiles panel.

Tap to talk: the Bridge picks a session id, asks the panel to capture
(assist.py), feeds the panel's PCM frames into Home Assistant's Assist
pipeline from the speech-to-text stage, asks the panel to stop once voice
activity ended, and plays the spoken answer on the panel's own speaker with
the existing speaker play command (speaker.py), decoded by the Bridge when the
panel takes Bridge streams (audio_stream.py). Only panels that announce the
``audio_input`` capability get the entity.

Assist screen: panels that announce ``assist_view`` get the run's stage and
texts (what was understood, the answer) as view commands, and "done" once the
spoken answer ended, so the panel can show the conversation.

Wake word: the panel detects microWakeWord models itself and reports them on
``{base}/stat/assist_wake``; the Bridge then runs the same flow as a button
press and passes the word's phrase to the pipeline. The words come from the
panel's retained status; Home Assistant's wake word settings send the
``wake_words`` command.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from time import monotonic

from homeassistant.components.assist_pipeline import (
    PipelineEvent,
    PipelineEventType,
    PipelineStage,
)
from homeassistant.components.assist_satellite import (
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteWakeWord,
)
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

# MQTT calls also reach panels on the direct link (link_mqtt.py).
from . import link_mqtt as mqtt
from .assist import (
    ASSIST_SATELLITES_KEY,
    MAX_ACTIVE_WAKE_WORDS,
    AudioSession,
    assist_audio_topic,
    assist_pipeline_select_unique_id,
    assist_satellite_unique_id,
    assist_status_topic,
    assist_vad_select_unique_id,
    assist_wake_topic,
    async_publish_assist_command,
    build_start_command,
    build_stop_command,
    build_view_command,
    build_mic_command,
    build_wake_words_command,
    new_session_id,
    parse_audio_frame,
    parse_status,
    parse_wake_event,
    view_error_kind,
    wake_choice,
    wake_word_info,
    wake_word_phrase,
)
from .capabilities import merged_capabilities_data, supports
from .command_channel import entry_pairing_key
from .const import DOMAIN
from .device_helpers import entry_base_topic, entry_device_id, entry_device_info, state_topic
from .local_camera import RateLimitedWarnings, parse_connected
from . import speaker

_LOGGER = logging.getLogger(__name__)

_WARNING_INTERVAL_S = 300.0
# The satellite returns to idle when the speaker never reports the answer
# finished (lost status, panel restart).
TTS_FINISH_TIMEOUT_S = 120.0
# The agent asked a question (continue_conversation): the panel listens again
# once its spoken answer ended, like Home Assistant's own satellites. A
# conversation keeps going for at most this many follow-ups in a row.
MAX_FOLLOW_UPS = 5
# The speaker hands I2S back to the microphone in this time after a clip.
FOLLOW_UP_DELAY_S = 0.3
# How long a follow-up waits for the answered run to finish its cleanup.
FOLLOW_UP_WAIT_S = 2.0
# Home Assistant converts the answer for the panel's decoder (mp3 or wav).
_TTS_OPTIONS = {"preferred_format": "mp3"}

_STOP_EVENTS = frozenset({
    PipelineEventType.STT_VAD_END,
    PipelineEventType.STT_END,
    PipelineEventType.ERROR,
    PipelineEventType.RUN_END,
})


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    # Microphone detection lives on the panel; the Bridge only mirrors the
    # announcement and never starts capture on panels that did not announce it.
    if supports(merged_capabilities_data(entry), "audio_input"):
        async_add_entities([HomeTilesAssistSatellite(entry, entry_base_topic(entry))])


def assist_satellite_for_entry(hass: HomeAssistant, entry_id: str):
    data = getattr(hass, "data", None)
    domain = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
    return domain.get(ASSIST_SATELLITES_KEY, {}).get(entry_id)


class HomeTilesAssistSatellite(AssistSatelliteEntity):
    """Streams the panel microphone into an Assist pipeline on demand."""

    _attr_has_entity_name = True
    _attr_translation_key = "assist"
    _attr_should_poll = False
    _attr_tts_options = _TTS_OPTIONS

    def __init__(self, entry: ConfigEntry, base_topic: str) -> None:
        self._entry_id = entry.entry_id
        self._paired = entry_pairing_key(entry) is not None
        self._attr_device_info = entry_device_info(entry)
        self._device_id = entry_device_id(entry)
        self._attr_unique_id = assist_satellite_unique_id(self._device_id)
        # The panel shows the run (assist_view.h): stage and texts.
        self._view = supports(merged_capabilities_data(entry), "assist_view")
        # The run the panel's screen shows; outlives the capture session
        # while the answer plays.
        self._view_id: str | None = None
        self._view_heard: str | None = None
        self._base = base_topic
        self._session: AudioSession | None = None
        self._run_task: asyncio.Task | None = None
        self._panel_online: bool | None = None
        self._mic_withdrawn = False
        self._tts_started = False
        self._tts_request_id: str | None = None
        self._tts_timer: asyncio.TimerHandle | None = None
        # The current answer asked for a reply; follow-ups in a row so far.
        self._follow_up = False
        self._follow_ups = 0
        # The delayed start of a follow-up (cancelled by a new run or removal).
        self._follow_up_task: asyncio.Task | None = None
        # The panel's speaker takes Bridge-decoded clips (its retained status).
        self._speaker_streams = False
        self._warnings = RateLimitedWarnings(_WARNING_INTERVAL_S)
        self._subscriptions = []
        self._attr_available = True
        # Wake words from the panel's retained status (ids).
        self._available_wake_words: list[str] = []
        self._active_wake_words: list[str] = []
        # The chosen word stays while the listen switch is off.
        self._chosen_wake_words: list[str] = []
        self._wake_listen = False

    # Assist selects ------------------------------------------------------

    # The select platform may load after this one, so the ids are looked up
    # per run. Without the selects Home Assistant uses its preferred pipeline
    # and default sensitivity.

    def _select_entity_id(self, unique_id: str) -> str | None:
        from homeassistant.helpers import entity_registry as er

        return er.async_get(self.hass).async_get_entity_id("select", DOMAIN, unique_id)

    @property
    def pipeline_entity_id(self) -> str | None:
        return self._select_entity_id(assist_pipeline_select_unique_id(self._device_id))

    @property
    def vad_sensitivity_entity_id(self) -> str | None:
        return self._select_entity_id(assist_vad_select_unique_id(self._device_id))

    # Lifecycle -----------------------------------------------------------

    def _refresh_available(self) -> None:
        # A cleared retained status means the panel withdrew its microphone.
        self._attr_available = self._panel_online is not False and not self._mic_withdrawn

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.data.setdefault(DOMAIN, {}).setdefault(
            ASSIST_SATELLITES_KEY, {})[self._entry_id] = self

        async def handle_audio(msg: mqtt.ReceiveMessage) -> None:
            frame = parse_audio_frame(msg.payload)
            if frame is None:
                if self._warnings.allow("invalid_frame", monotonic()):
                    _LOGGER.warning("HomeTiles Assist audio frame ignored for %s: invalid frame",
                                    self._base)
                return
            session = self._session
            if session is not None:
                session.feed(frame)

        async def handle_status(msg: mqtt.ReceiveMessage) -> None:
            if not msg.payload:
                self._mic_withdrawn = True
                self._end_session()
            else:
                status = parse_status(msg.payload)
                if status is None:
                    if self._warnings.allow("invalid_status", monotonic()):
                        _LOGGER.warning("HomeTiles Assist status ignored for %s: invalid payload",
                                        self._base)
                    return
                self._mic_withdrawn = False
                if status["wake_words"] is not None:
                    self._available_wake_words = status["wake_words"]
                if status["active_wake_words"] is not None:
                    self._active_wake_words = [
                        word for word in status["active_wake_words"]
                        if word in self._available_wake_words]
                choice = wake_choice(status)
                if choice is not None:
                    self._chosen_wake_words, self._wake_listen = choice
                session = self._session
                if (session is not None and status["id"] == session.session_id
                        and status["state"] == "idle"):
                    # The panel ended capture on its own (max_ms, error).
                    if status["error"] and self._warnings.allow(
                            f"session_{status['error']}", monotonic()):
                        _LOGGER.warning("HomeTiles Assist capture on %s ended: %s",
                                        self._base, status["error"])
                    session.finish()
            self._refresh_available()
            self.async_write_ha_state()

        async def handle_wake(msg: mqtt.ReceiveMessage) -> None:
            event = parse_wake_event(msg.payload, self._available_wake_words)
            if event is None:
                if self._warnings.allow("invalid_wake", monotonic()):
                    _LOGGER.warning("HomeTiles Assist wake event ignored for %s: invalid or "
                                    "unknown wake word", self._base)
                return
            if self._run_active():
                _LOGGER.debug("HomeTiles Assist wake event %s on %s ignored: run active",
                              event.sequence, self._base)
                return
            try:
                await self._async_start_run(wake_word_phrase(event.word))
            except HomeAssistantError as err:
                if self._warnings.allow("wake_start_failed", monotonic()):
                    _LOGGER.warning("HomeTiles Assist wake word on %s not started: %s",
                                    self._base, err.translation_key or err)

        async def handle_connected(msg: mqtt.ReceiveMessage) -> None:
            online = parse_connected(msg.payload)
            if online is None:
                return
            self._panel_online = online
            if not online:
                self._end_session()
                self._finish_tts()
            self._refresh_available()
            self.async_write_ha_state()

        async def handle_speaker_status(msg: mqtt.ReceiveMessage) -> None:
            if not msg.payload:
                return
            status = speaker.parse_status(msg.payload)
            if status is not None:
                self._speaker_streams = status["stream"]
            if self._tts_request_id is None:
                return
            if (status is not None and status["id"] == self._tts_request_id
                    and status["state"] == "idle"):
                if status["error"]:
                    _LOGGER.warning("HomeTiles Assist answer on %s could not be played: %s",
                                    self._base, status["error"])
                self._finish_tts(played=not status["error"])

        # Microphone frames are binary; encoding=None delivers them as bytes.
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, assist_audio_topic(self._base), handle_audio, qos=0, encoding=None))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, assist_status_topic(self._base), handle_status, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, assist_wake_topic(self._base), handle_wake, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, state_topic(self._base, "connected"), handle_connected, qos=0))
        self._subscriptions.append(await mqtt.async_subscribe(
            self.hass, speaker.audio_status_topic(self._base), handle_speaker_status, qos=0))

    async def async_will_remove_from_hass(self) -> None:
        satellites = self.hass.data.get(DOMAIN, {}).get(ASSIST_SATELLITES_KEY, {})
        if satellites.get(self._entry_id) is self:
            satellites.pop(self._entry_id, None)
        # Never leave the microphone running: the cancelled run sends stop.
        self._cancel_follow_up()
        await self._async_cancel_run()
        self._end_view()
        self._cancel_tts_timer()
        self._tts_request_id = None
        for unsubscribe in self._subscriptions:
            unsubscribe()
        self._subscriptions.clear()
        await super().async_will_remove_from_hass()

    # Configuration -------------------------------------------------------

    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        available = []
        for word_id in self._available_wake_words:
            phrase, languages = wake_word_info(word_id)
            available.append(AssistSatelliteWakeWord(
                id=word_id, wake_word=phrase, trained_languages=languages))
        return AssistSatelliteConfiguration(
            available_wake_words=available,
            active_wake_words=list(self._active_wake_words),
            max_active_wake_words=MAX_ACTIVE_WAKE_WORDS)

    async def async_set_configuration(self, config: AssistSatelliteConfiguration) -> None:
        await self.async_set_active_wake_words(config.active_wake_words)

    async def async_set_active_wake_words(self, words: list[str]) -> None:
        """Home Assistant's own wake word setting: words listen, none stops."""
        active = [word for word in words
                  if word in self._available_wake_words][:MAX_ACTIVE_WAKE_WORDS]
        await self._async_send_wake_words(active)
        # The panel's retained status confirms it; until then show the request.
        self._active_wake_words = active
        if active:
            self._chosen_wake_words = active
        self._wake_listen = bool(active)
        self.async_write_ha_state()

    @property
    def wake_listen(self) -> bool:
        return self._wake_listen

    async def async_set_wake_listen(self, enabled: bool) -> None:
        """The Listening switch; the chosen word stays while it is off."""
        words: list[str] = []
        if enabled:
            words = (self._chosen_wake_words or self._available_wake_words)[:1]
        await self._async_send_wake_words(words, enabled)
        self._active_wake_words = words
        self._wake_listen = enabled
        self.async_write_ha_state()

    async def async_choose_wake_word(self, word: str) -> None:
        """The Wake word select: changes the word, not the listen switch."""
        if word not in self._available_wake_words:
            raise HomeAssistantError(f"Unknown wake word: {word}")
        await self._async_send_wake_words([word], self._wake_listen)
        self._chosen_wake_words = [word]
        if self._wake_listen:
            self._active_wake_words = [word]
        self.async_write_ha_state()

    async def _async_send_wake_words(self, words: list[str], listen: bool | None = None) -> None:
        try:
            sent = await self._async_send_assist(build_wake_words_command(words, listen))
        except Exception:  # noqa: BLE001 - reported as not connected below
            _LOGGER.debug("HomeTiles Assist wake words for %s failed", self._base,
                          exc_info=True)
            sent = False
        if not sent:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_not_connected")

    async def async_set_microphone(self, enabled: bool) -> None:
        """Turn the panel's microphone privacy switch on or off.

        The panel accepts "on" only over the encrypted channel, so an
        unpaired panel can only be switched on at the panel itself.
        """
        if enabled and not self._paired:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="microphone_needs_pairing")
        try:
            sent = await self._async_send_assist(build_mic_command(enabled))
        except Exception:  # noqa: BLE001 - reported as not connected below
            _LOGGER.debug("HomeTiles microphone switch for %s failed", self._base,
                          exc_info=True)
            sent = False
        if not sent:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_not_connected")

    # Commands ------------------------------------------------------------

    def _bridge(self):
        data = getattr(self.hass, "data", None)
        domain = data.get(DOMAIN, {}) if isinstance(data, dict) else {}
        return domain.get("entries", {}).get(self._entry_id)

    async def _async_send_assist(self, payload: str) -> bool:
        # Commands, never retained state; sealed when the panel is paired.
        return await async_publish_assist_command(
            self.hass, self._bridge(), self._paired, self._base, payload, mqtt.async_publish)

    async def _async_publish_stop(self, session_id: str) -> None:
        try:
            await self._async_send_assist(build_stop_command(session_id))
        except Exception as err:  # noqa: BLE001 - a lost stop is covered by max_ms
            _LOGGER.debug("HomeTiles Assist stop for %s failed: %s", self._base, err)

    @callback
    def _request_stop(self, session: AudioSession | None) -> None:
        """Ask the panel to stop capturing, at most once per session."""
        if session is None or session.stop_sent:
            return
        session.stop_sent = True
        session.finish()
        self.hass.async_create_task(self._async_publish_stop(session.session_id))

    @callback
    def _end_session(self) -> None:
        if self._session is not None:
            self._session.finish()

    async def _async_cancel_run(self) -> None:
        task = self._run_task
        self._run_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _run_active(self) -> bool:
        task = self._run_task
        return task is not None and not task.done()

    async def async_start_tap_to_talk(self) -> None:
        """Start one Assist run with the panel microphone."""
        await self._async_start_run(None)

    async def _async_start_run(self, phrase: str | None, follow_up: bool = False) -> None:
        """Start one Assist run; a wake word passes its phrase to the pipeline.

        ``follow_up``: the agent's last answer asked a question; a wake word or
        a press starts a new count.
        """
        self._follow_ups = self._follow_ups + 1 if follow_up else 0
        self._follow_up = False
        if not follow_up:
            # A wake word or a press replaces a follow-up still waiting.
            self._cancel_follow_up()
        if not self.available:
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_unavailable")
        # A new press replaces the running conversation; its stop goes out
        # before the new start.
        await self._async_cancel_run()
        self._finish_tts()
        session = AudioSession(new_session_id())
        self._session = session
        try:
            sent = await self._async_send_assist(build_start_command(session.session_id))
        except Exception:
            sent = False
            _LOGGER.debug("HomeTiles Assist start for %s failed", self._base, exc_info=True)
        if not sent:
            session.stop_sent = True
            session.finish()
            if self._session is session:
                self._session = None
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="assist_not_connected")
        self._view_id = session.session_id
        self._view_heard = None
        self._send_view("listening")
        self._run_task = self.hass.async_create_background_task(
            self._async_run(session, phrase), f"{DOMAIN}_assist_{self._entry_id}")

    async def _async_run(self, session: AudioSession, phrase: str | None = None) -> None:
        self._tts_started = False
        try:
            await self.async_accept_pipeline_from_satellite(
                session.stream(),
                start_stage=PipelineStage.STT,
                end_stage=PipelineStage.TTS,
                wake_word_phrase=phrase,
            )
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - one failed run must not stick
            _LOGGER.warning("HomeTiles Assist run on %s failed: %s", self._base, err)
        finally:
            session.finish()
            if not session.stop_sent:
                session.stop_sent = True
                await self._async_publish_stop(session.session_id)
            if self._session is session:
                self._session = None
            # A run that failed or was cancelled before its answer still
            # closes the panel's screen (an answer playing ends it later).
            if (self._view_id == session.session_id and self._tts_request_id is None
                    and not self._tts_started):
                self._end_view()

    # Pipeline ------------------------------------------------------------

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        event_type = event.type
        if event_type in _STOP_EVENTS:
            self._request_stop(self._session)
        if event_type is PipelineEventType.INTENT_END:
            output = (event.data or {}).get("intent_output") or {}
            self._follow_up = output.get("continue_conversation") is True
        self._view_pipeline_event(event)
        if event_type is PipelineEventType.TTS_START:
            self._tts_started = True
        elif event_type is PipelineEventType.TTS_END:
            self._play_answer(event.data)
        elif event_type is PipelineEventType.RUN_END:
            if self._tts_started and self._tts_request_id is None:
                # TTS started but produced nothing playable.
                self._finish_tts()
            elif self._tts_request_id is None:
                # Nothing to play (error, no TTS stage): the run is over.
                self._end_view()
            self._tts_started = False

    # Assist screen -------------------------------------------------------

    def _view_pipeline_event(self, event: PipelineEvent) -> None:
        if self._view_id is None:
            return
        data = event.data or {}
        event_type = event.type
        if event_type is PipelineEventType.STT_END:
            text = ((data.get("stt_output") or {}).get("text") or "").strip()
            if text:
                self._view_heard = text
                self._send_view("processing", heard=text)
        elif event_type is PipelineEventType.INTENT_END:
            answer = _answer_text(data)
            if answer:
                self._send_view("responding", heard=self._view_heard, answer=answer)
        elif event_type is PipelineEventType.ERROR:
            self._send_view("error", error=view_error_kind(data.get("code")))

    def _send_view(self, stage: str, **texts) -> None:
        view_id = self._view_id
        if not self._view or view_id is None or self.hass is None:
            return
        try:
            payload = build_view_command(view_id, stage, **texts)
        except ValueError as err:
            _LOGGER.debug("HomeTiles Assist view for %s not built: %s", self._base, err)
            return
        self.hass.async_create_task(self._async_send_view(payload))

    async def _async_send_view(self, payload: str) -> None:
        try:
            await self._async_send_assist(payload)
        except Exception as err:  # noqa: BLE001 - the panel's screen times out
            _LOGGER.debug("HomeTiles Assist view for %s failed: %s", self._base, err)

    @callback
    def _end_view(self) -> None:
        """The run and its spoken answer ended: the panel closes its screen."""
        if self._view_id is None:
            return
        self._send_view("done")
        self._view_id = None
        self._view_heard = None

    @callback
    def _play_answer(self, data) -> None:
        tts_output = (data or {}).get("tts_output") or {}
        path = tts_output.get("url")
        if not path:
            return
        # Relative /api/tts_proxy URLs become absolute and signed, so the
        # panel can fetch them without a token of its own.
        url = async_process_play_media_url(self.hass, path)
        error = speaker.playable_url_error(url, https_allowed=True)
        if error is not None:
            if self._warnings.allow(f"tts_{error}", monotonic()):
                _LOGGER.warning("HomeTiles Assist answer for %s not sent: %s", self._base, error)
            return
        request_id = speaker.new_request_id()
        self._tts_request_id = request_id
        self._arm_tts_timer(request_id)
        self.hass.async_create_task(self._async_send_answer(request_id, url))

    async def _async_send_answer(self, request_id: str, url: str) -> None:
        bridge = self._bridge()
        try:
            create_stream = getattr(bridge, "async_create_audio_stream", None)
            if self._speaker_streams and create_stream is not None:
                try:
                    url = await create_stream(url, speaker.AUDIO_RATE_SPEECH)
                except ValueError as err:
                    # No camera listener: the panel downloads the answer itself.
                    _LOGGER.debug("HomeTiles Assist answer for %s without the Bridge stream: %s",
                                  self._base, err)
            sent = await speaker.async_publish_audio_command(
                self.hass, bridge, self._paired, self._base,
                speaker.build_play_command(request_id, url), mqtt.async_publish)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("HomeTiles Assist answer for %s failed: %s", self._base, err)
            sent = False
        if not sent and self._tts_request_id == request_id:
            self._finish_tts()

    def _arm_tts_timer(self, request_id: str) -> None:
        self._cancel_tts_timer()

        def timed_out() -> None:
            self._tts_timer = None
            if self._tts_request_id == request_id:
                self._finish_tts()

        self._tts_timer = asyncio.get_running_loop().call_later(TTS_FINISH_TIMEOUT_S, timed_out)

    def _cancel_tts_timer(self) -> None:
        if self._tts_timer is not None:
            self._tts_timer.cancel()
            self._tts_timer = None

    @callback
    def _finish_tts(self, played: bool = False) -> None:
        """The spoken answer ended (played, failed, timed out or replaced).

        ``played``: the panel played it to the end. When that answer asked a
        question, the panel listens for the reply.
        """
        had_answer = self._tts_request_id is not None or self._tts_started
        follow_up = played and had_answer and self._follow_up
        self._follow_up = False
        self._tts_request_id = None
        self._tts_started = False
        self._cancel_tts_timer()
        self._end_view()
        if had_answer and self.hass is not None:
            self.tts_response_finished()
        if follow_up and self.hass is not None:
            if self._follow_ups >= MAX_FOLLOW_UPS:
                _LOGGER.debug("HomeTiles Assist on %s: %s follow-ups in a row, not listening again",
                              self._base, self._follow_ups)
                return
            self._cancel_follow_up()
            self._follow_up_task = self.hass.async_create_task(self._async_follow_up())

    def _cancel_follow_up(self) -> None:
        task = self._follow_up_task
        self._follow_up_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    async def _async_follow_up(self) -> None:
        """Listen for the reply to the agent's question."""
        await asyncio.sleep(FOLLOW_UP_DELAY_S)
        # The answered run may still be sending its stop: wait for it briefly.
        task = self._run_task
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=FOLLOW_UP_WAIT_S)
        if self._run_active() or not self.available:
            return
        try:
            await self._async_start_run(None, follow_up=True)
        except HomeAssistantError as err:
            if self._warnings.allow("follow_up_failed", monotonic()):
                _LOGGER.warning("HomeTiles Assist follow-up on %s not started: %s",
                                self._base, err.translation_key or err)


def _answer_text(data) -> str:
    """The spoken answer of an intent-end event (ConversationResult.as_dict)."""
    response = (data.get("intent_output") or {}).get("response") or {}
    speech = response.get("speech") or {}
    for kind in ("plain", "ssml"):
        text = (speech.get(kind) or {}).get("speech")
        if isinstance(text, str) and kind == "ssml":
            # The panel shows the words, not the markup.
            text = " ".join(_SSML_TAG.sub(" ", text).split())
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


_SSML_TAG = re.compile(r"<[^>]*>")
