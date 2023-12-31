import asyncio
import json
import unittest
from typing import Any, Awaitable, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from bidict import bidict

from hummingbot.client.config.client_config_map import ClientConfigMap
from hummingbot.client.config.config_helpers import ClientConfigAdapter
from hummingbot.connector.exchange.woo_x import woo_x_constants as CONSTANTS
from hummingbot.connector.exchange.woo_x.woo_x_api_user_stream_data_source import WooXAPIUserStreamDataSource
from hummingbot.connector.exchange.woo_x.woo_x_auth import WooXAuth
from hummingbot.connector.exchange.woo_x.woo_x_exchange import WooXExchange
from hummingbot.connector.test_support.network_mocking_assistant import NetworkMockingAssistant
from hummingbot.connector.time_synchronizer import TimeSynchronizer
from hummingbot.core.api_throttler.async_throttler import AsyncThrottler


class WooXUserStreamDataSourceUnitTests(unittest.TestCase):
    # the level is required to receive logs from the data source logger
    level = 0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.ev_loop = asyncio.get_event_loop()
        cls.base_asset = "COINALPHA"
        cls.quote_asset = "HBOT"
        cls.trading_pair = f"{cls.base_asset}-{cls.quote_asset}"
        cls.ex_trading_pair = cls.base_asset + cls.quote_asset
        cls.domain = "woo_x"

        cls.listen_key = "TEST_LISTEN_KEY"

    def setUp(self) -> None:
        super().setUp()
        self.log_records = []
        self.listening_task: Optional[asyncio.Task] = None
        self.mocking_assistant = NetworkMockingAssistant()

        self.throttler = AsyncThrottler(rate_limits=CONSTANTS.RATE_LIMITS)
        self.mock_time_provider = MagicMock()
        self.mock_time_provider.time.return_value = 1000
        self.auth = WooXAuth(api_key="TEST_API_KEY", secret_key="TEST_SECRET", time_provider=self.mock_time_provider)
        self.time_synchronizer = TimeSynchronizer()
        self.time_synchronizer.add_time_offset_ms_sample(0)

        client_config_map = ClientConfigAdapter(ClientConfigMap())
        self.connector = WooXExchange(
            client_config_map=client_config_map,
            public_api_key="",
            secret_api_key="",
            application_id="",
            trading_pairs=[],
            trading_required=False,
            domain=self.domain)
        self.connector._web_assistants_factory._auth = self.auth

        self.data_source = WooXAPIUserStreamDataSource(
            auth=self.auth,
            trading_pairs=[self.trading_pair],
            connector=self.connector,
            api_factory=self.connector._web_assistants_factory,
            domain=self.domain
        )

        self.data_source.logger().setLevel(1)
        self.data_source.logger().addHandler(self)

        self.resume_test_event = asyncio.Event()

        self.connector._set_trading_pair_symbol_map(bidict({self.ex_trading_pair: self.trading_pair}))

    def tearDown(self) -> None:
        self.listening_task and self.listening_task.cancel()
        super().tearDown()

    def handle(self, record):
        self.log_records.append(record)

    def _is_logged(self, log_level: str, message: str) -> bool:
        return any(record.levelname == log_level and record.getMessage() == message
                   for record in self.log_records)

    def _raise_exception(self, exception_class):
        raise exception_class

    def _create_exception_and_unlock_test_with_event(self, exception):
        self.resume_test_event.set()
        raise exception

    def _create_return_value_and_unlock_test_with_event(self, value):
        self.resume_test_event.set()
        return value

    def async_run_with_timeout(self, coroutine: Awaitable, timeout: float = 1):
        ret = self.ev_loop.run_until_complete(asyncio.wait_for(coroutine, timeout))
        return ret

    def _error_response(self) -> Dict[str, Any]:
        resp = {
            "code": "ERROR CODE",
            "msg": "ERROR MESSAGE"
        }

        return resp

    def _user_update_event(self):
        # Balance Update
        resp = {
            "e": "balanceUpdate",
            "E": 1573200697110,
            "a": "BTC",
            "d": "100.00000000",
            "T": 1573200697068
        }
        return json.dumps(resp)

    def _successfully_subscribed_event(self):
        resp = {
            "result": None,
            "id": 1
        }
        return resp

    def _authentication_response(self, success: bool) -> str:
        return json.dumps({
            "id": "auth",
            "event": "auth",
            "success": success,
            "ts": 1686526749230,
            **({} if success else {"errorMsg": "sample error message"})
        })

    def _subscription_response(self, success: bool, channel: str) -> str:
        return json.dumps({
            'id': channel,
            'event': 'subscribe',
            'success': success,
            'ts': 1686527628871
        })

    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    def test_listening_process_authenticates_and_subscribes_to_events(self, ws_connect_mock):
        messages = asyncio.Queue()

        ws_connect_mock.return_value = self.mocking_assistant.create_websocket_mock()

        initial_last_recv_time = self.data_source.last_recv_time

        # Add the authentication response for the websocket
        self.mocking_assistant.add_websocket_aiohttp_message(
            ws_connect_mock.return_value,
            self._authentication_response(True)
        )

        self.mocking_assistant.add_websocket_aiohttp_message(
            ws_connect_mock.return_value,
            self._subscription_response(
                True,
                'executionreport'
            )
        )

        self.mocking_assistant.add_websocket_aiohttp_message(
            ws_connect_mock.return_value,
            self._subscription_response(
                True,
                'balance'
            )
        )

        self.listening_task = asyncio.get_event_loop().create_task(
            self.data_source.listen_for_user_stream(messages)
        )

        self.mocking_assistant.run_until_all_aiohttp_messages_delivered(ws_connect_mock.return_value)

        self.assertTrue(
            self._is_logged("INFO", "Subscribed to private account and orders channels...")
        )

        sent_messages = self.mocking_assistant.json_messages_sent_through_websocket(ws_connect_mock.return_value)

        self.assertEqual(3, len(sent_messages))

        for n, id in enumerate(['auth', 'executionreport', 'balance']):
            self.assertEqual(sent_messages[n]['id'], id)

        self.assertGreater(self.data_source.last_recv_time, initial_last_recv_time)

    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    def test_listen_for_user_stream_authentication_failure(self, ws_connect_mock):
        messages = asyncio.Queue()

        ws_connect_mock.return_value = self.mocking_assistant.create_websocket_mock()

        self.mocking_assistant.add_websocket_aiohttp_message(
            ws_connect_mock.return_value,
            self._authentication_response(False)
        )

        self.listening_task = asyncio.get_event_loop().create_task(
            self.data_source.listen_for_user_stream(messages)
        )

        self.mocking_assistant.run_until_all_aiohttp_messages_delivered(ws_connect_mock.return_value)

        self.assertTrue(
            self._is_logged(
                "ERROR",
                f"Error authenticating the private websocket connection: {self._authentication_response(False)}"
            )
        )

        self.assertTrue(
            self._is_logged(
                "ERROR",
                "Unexpected error while listening to user stream. Retrying after 5 seconds..."
            )
        )

    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    def test_listen_for_user_stream_does_not_queue_empty_payload(self, mock_ws):
        mock_ws.return_value = self.mocking_assistant.create_websocket_mock()

        self.mocking_assistant.add_websocket_aiohttp_message(
            mock_ws.return_value, ""
        )

        msg_queue = asyncio.Queue()
        self.listening_task = self.ev_loop.create_task(
            self.data_source.listen_for_user_stream(msg_queue)
        )

        self.mocking_assistant.run_until_all_aiohttp_messages_delivered(mock_ws.return_value)

        self.assertEqual(0, msg_queue.qsize())

    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    def test_listen_for_user_stream_connection_failed(self, mock_ws):
        mock_ws.side_effect = lambda *arg, **kwars: self._create_exception_and_unlock_test_with_event(
            Exception("TEST ERROR.")
        )

        msg_queue = asyncio.Queue()

        self.listening_task = self.ev_loop.create_task(
            self.data_source.listen_for_user_stream(msg_queue)
        )

        self.async_run_with_timeout(self.resume_test_event.wait())

        self.assertTrue(
            self._is_logged(
                "ERROR",
                "Unexpected error while listening to user stream. Retrying after 5 seconds..."
            )
        )

    @patch("aiohttp.ClientSession.ws_connect", new_callable=AsyncMock)
    def test_listening_process_canceled_on_cancel_exception(self, mock_ws):
        messages = asyncio.Queue()

        mock_ws.side_effect = asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            self.listening_task = asyncio.get_event_loop().create_task(
                self.data_source.listen_for_user_stream(messages)
            )

            self.async_run_with_timeout(self.listening_task)
