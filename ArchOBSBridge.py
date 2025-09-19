#!/usr/bin/env python3
"""
Archipelago to OBS Bridge - Full Server Observer
Connects to an Archipelago server as an observer and sends ALL events to OBS via WebSocket
Very untested
"""

import asyncio
import json
import logging
import os
from typing import Dict, Any, Optional, List
import websockets
from websockets.exceptions import ConnectionClosed, InvalidMessage
import obsws_python as obs

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class ArchipelagoOBSBridge:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.archipelago_ws = None
        self.obs_client = None
        self.running = False

        # Store server state
        self.connected_players = {}
        self.player_names = {}
        self.location_names = {}
        self.item_names = {}
        self.game_data = {}
        self.room_info = {}

    async def connect_obs(self):
        """Connect to OBS WebSocket"""
        try:
            self.obs_client = obs.ReqClient(
                host=self.config.get('obs_host', 'localhost'),
                port=self.config.get('obs_port', 4455),
                password=self.config.get('obs_password', '')
            )
            logger.info("Connected to OBS WebSocket")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to OBS: {e}")
            return False

    async def connect_archipelago(self):
        """Connect to Archipelago server as observer"""
        try:
            uri = f"wss://{self.config['archipelago_host']}:{self.config['archipelago_port']}"
            self.archipelago_ws = await websockets.connect(uri)
            logger.info(f"Connected to Archipelago server at {uri}")

            # Connect as observer to see ALL events
            connect_packet = {
                "cmd": "Connect",
                "password": self.config.get('archipelago_password', ''),
                "game": "Observer",  # Observer can see all events
                "name": self.config.get('bot_name', 'OBS_Observer_Bot'),
                "uuid": self.config.get('uuid', ''),
                "version": {"major": 0, "minor": 4, "build": 6},
                "items_handling": 0b000,  # No item handling
                "tags": ["Tracker", "Observer"]
            }

            await self.archipelago_ws.send(json.dumps([connect_packet]))
            logger.info("Sent observer connect packet to Archipelago")
            return True

        except Exception as e:
            logger.error(f"Failed to connect to Archipelago: {e}")
            return False

    async def request_data_package(self):
        """Request data package for name resolution"""
        data_package_request = {
            "cmd": "GetDataPackage",
            "games": []  # Empty means all games
        }
        await self.archipelago_ws.send(json.dumps([data_package_request]))
        logger.info("Requested data package for all games")

    def resolve_player_name(self, player_id: int) -> str:
        """Get player name from ID"""
        return self.player_names.get(player_id, f"Player_{player_id}")

    def resolve_item_name(self, item_id: int, game: str = None) -> str:
        """Get item name from ID and game"""
        if game and game in self.item_names:
            return self.item_names[game].get(item_id, f"Item_{item_id}")
        return f"Item_{item_id}"

    def resolve_location_name(self, location_id: int, game: str = None) -> str:
        """Get location name from ID and game"""
        if game and game in self.location_names:
            return self.location_names[game].get(location_id, f"Location_{location_id}")
        return f"Location_{location_id}"

    async def handle_archipelago_message(self, message_data: list):
        """Process ALL messages from Archipelago server"""
        for packet in message_data:
            cmd = packet.get('cmd')

            if cmd == 'Connected':
                await self.handle_connected(packet)
            elif cmd == 'RoomInfo':
                await self.handle_room_info(packet)
            elif cmd == 'ConnectionRefused':
                await self.handle_connection_refused(packet)
            elif cmd == 'ReceivedItems':
                await self.handle_received_items(packet)
            elif cmd == 'LocationInfo':
                await self.handle_location_info(packet)
            elif cmd == 'RoomUpdate':
                await self.handle_room_update(packet)
            elif cmd == 'PrintJSON':
                await self.handle_print_json(packet)
            elif cmd == 'DataPackage':
                await self.handle_data_package(packet)
            elif cmd == 'Bounced':
                await self.handle_bounced(packet)
            elif cmd == 'InvalidPacket':
                await self.handle_invalid_packet(packet)
            else:
                logger.debug(f"Unknown command: {cmd}")

    async def handle_connected(self, packet):
        """Handle successful connection to Archipelago"""
        slot_data = packet.get('slot_data', {})
        slot_info = packet.get('slot_info', {})

        # Store player information
        for slot_id, player_info in slot_info.items():
            self.player_names[int(slot_id)] = player_info.get('name', f'Player_{slot_id}')
            self.connected_players[int(slot_id)] = {
                'name': player_info.get('name'),
                'game': player_info.get('game'),
                'type': player_info.get('type', 0)
            }

        logger.info(f"Observer connected! Monitoring {len(slot_info)} players")

        # Request data package after connection
        await self.request_data_package()

        await self.trigger_obs_event("server_connected", {
            "player_count": len(slot_info),
            "players": self.connected_players,
            "slot_data": slot_data
        })

    async def handle_room_info(self, packet):
        """Handle room information"""
        self.room_info = packet

        await self.trigger_obs_event("room_info", {
            "seed_name": packet.get('seed_name'),
            "permissions": packet.get('permissions', {}),
            "hint_cost": packet.get('hint_cost', 10),
            "location_check_points": packet.get('location_check_points', 1),
            "players": packet.get('players', []),
            "version": packet.get('version', {}),
            "generator_version": packet.get('generator_version', {}),
            "forfeit_mode": packet.get('forfeit_mode', 'goal'),
            "remaining_mode": packet.get('remaining_mode', 'goal'),
            "hint_points": packet.get('hint_points', 0)
        })

    async def handle_connection_refused(self, packet):
        """Handle connection refusal"""
        errors = packet.get('errors', [])
        logger.error(f"Connection refused: {errors}")

        await self.trigger_obs_event("connection_refused", {
            "errors": errors
        })

    async def handle_received_items(self, packet):
        """Handle any player receiving items"""
        items = packet.get('items', [])
        index = packet.get('index', 0)

        for i, item in enumerate(items):
            receiving_player = self.resolve_player_name(item.get('player', 0))
            sending_player = self.resolve_player_name(item.get('sender', 0)) if 'sender' in item else "Server"
            item_name = self.resolve_item_name(item.get('item', 0))
            location_name = self.resolve_location_name(item.get('location', 0))

            await self.trigger_obs_event("item_received", {
                "receiving_player": receiving_player,
                "sending_player": sending_player,
                "item_name": item_name,
                "location_name": location_name,
                "item_id": item.get('item'),
                "location_id": item.get('location'),
                "player_id": item.get('player'),
                "sender_id": item.get('sender'),
                "flags": item.get('flags', 0),
                "index": index + i
            })

    async def handle_location_info(self, packet):
        """Handle location checks from any player"""
        locations = packet.get('locations', [])

        for location in locations:
            player_name = self.resolve_player_name(location.get('player', 0))
            item_name = self.resolve_item_name(location.get('item', 0))
            location_name = self.resolve_location_name(location.get('location', 0))

            await self.trigger_obs_event("location_checked", {
                "player_name": player_name,
                "item_name": item_name,
                "location_name": location_name,
                "player_id": location.get('player'),
                "item_id": location.get('item'),
                "location_id": location.get('location')
            })

    async def handle_room_update(self, packet):
        """Handle room updates (player connections/disconnections)"""
        await self.trigger_obs_event("room_update", packet)

    async def handle_print_json(self, packet):
        """Handle ALL chat/print messages from the server"""
        data = packet.get('data', [])
        message_type = packet.get('type', 'Chat')

        # Parse the message data for player names, items, etc.
        parsed_data = self.parse_print_json_data(data)

        if message_type == 'ItemSend':
            await self.handle_global_item_send(parsed_data, data)
        elif message_type == 'ItemCheat':
            await self.handle_global_item_found(parsed_data, data)
        elif message_type == 'Hint':
            await self.handle_global_hint(parsed_data, data)
        elif message_type == 'Join':
            await self.handle_global_player_join(parsed_data, data)
        elif message_type == 'Part':
            await self.handle_global_player_part(parsed_data, data)
        elif message_type == 'Chat':
            await self.handle_global_chat(parsed_data, data)
        elif message_type == 'ServerChat':
            await self.handle_server_chat(parsed_data, data)
        elif message_type == 'Tutorial':
            await self.handle_tutorial(parsed_data, data)
        elif message_type == 'TagsChanged':
            await self.handle_tags_changed(parsed_data, data)
        elif message_type == 'CommandResult':
            await self.handle_command_result(parsed_data, data)
        elif message_type == 'AdminCommandResult':
            await self.handle_admin_command_result(parsed_data, data)
        elif message_type == 'Goal':
            await self.handle_goal_completion(parsed_data, data)
        elif message_type == 'Release':
            await self.handle_release(parsed_data, data)
        elif message_type == 'Collect':
            await self.handle_collect(parsed_data, data)
        elif message_type == 'Countdown':
            await self.handle_countdown(parsed_data, data)
        else:
            # Catch any other message types
            await self.trigger_obs_event("unknown_message", {
                "type": message_type,
                "parsed_data": parsed_data,
                "raw_data": data
            })

    def parse_print_json_data(self, data: List[Dict]) -> Dict[str, Any]:
        """Parse PrintJSON data to extract meaningful information"""
        parsed = {
            "text": "",
            "players": [],
            "items": [],
            "locations": []
        }

        for part in data:
            if isinstance(part, dict):
                if part.get('type') == 'player_id':
                    player_id = part.get('text', 0)
                    if isinstance(player_id, int):
                        parsed['players'].append({
                            'id': player_id,
                            'name': self.resolve_player_name(player_id)
                        })
                elif part.get('type') == 'item_id':
                    item_id = part.get('text', 0)
                    if isinstance(item_id, int):
                        parsed['items'].append({
                            'id': item_id,
                            'name': self.resolve_item_name(item_id),
                            'flags': part.get('flags', 0)
                        })
                elif part.get('type') == 'location_id':
                    location_id = part.get('text', 0)
                    if isinstance(location_id, int):
                        parsed['locations'].append({
                            'id': location_id,
                            'name': self.resolve_location_name(location_id)
                        })
                else:
                    parsed['text'] += str(part.get('text', ''))
            else:
                parsed['text'] += str(part)

        return parsed

    async def handle_data_package(self, packet):
        """Handle data package for name resolution"""
        data_package = packet.get('data', {})
        games = data_package.get('games', {})

        for game_name, game_data in games.items():
            if game_name not in self.game_data:
                self.game_data[game_name] = game_data

                # Store item and location names for this game
                if game_name not in self.item_names:
                    self.item_names[game_name] = {}
                if game_name not in self.location_names:
                    self.location_names[game_name] = {}

                # Parse item names
                if 'item_name_to_id' in game_data:
                    for item_name, item_id in game_data['item_name_to_id'].items():
                        self.item_names[game_name][item_id] = item_name

                # Parse location names
                if 'location_name_to_id' in game_data:
                    for location_name, location_id in game_data['location_name_to_id'].items():
                        self.location_names[game_name][location_id] = location_name

        logger.info(f"Updated data package for {len(games)} games")

        await self.trigger_obs_event("data_package_updated", {
            "games": list(games.keys()),
            "total_items": sum(len(self.item_names.get(g, {})) for g in games.keys()),
            "total_locations": sum(len(self.location_names.get(g, {})) for g in games.keys())
        })

    # Global event handlers
    async def handle_global_item_send(self, parsed_data, raw_data):
        """Handle item send events from any player"""
        await self.trigger_obs_event("global_item_send", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_global_item_found(self, parsed_data, raw_data):
        """Handle item found events from any player"""
        await self.trigger_obs_event("global_item_found", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_global_hint(self, parsed_data, raw_data):
        """Handle hint events"""
        await self.trigger_obs_event("global_hint", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_global_player_join(self, parsed_data, raw_data):
        """Handle player join events"""
        await self.trigger_obs_event("global_player_join", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_global_player_part(self, parsed_data, raw_data):
        """Handle player leave events"""
        await self.trigger_obs_event("global_player_part", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_global_chat(self, parsed_data, raw_data):
        """Handle chat messages"""
        await self.trigger_obs_event("global_chat", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_server_chat(self, parsed_data, raw_data):
        """Handle server chat messages"""
        await self.trigger_obs_event("server_chat", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_goal_completion(self, parsed_data, raw_data):
        """Handle goal completion events"""
        await self.trigger_obs_event("goal_completed", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_release(self, parsed_data, raw_data):
        """Handle release events"""
        await self.trigger_obs_event("player_released", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_collect(self, parsed_data, raw_data):
        """Handle collect events"""
        await self.trigger_obs_event("player_collected", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_countdown(self, parsed_data, raw_data):
        """Handle countdown events"""
        await self.trigger_obs_event("countdown", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_tutorial(self, parsed_data, raw_data):
        """Handle tutorial messages"""
        await self.trigger_obs_event("tutorial", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_tags_changed(self, parsed_data, raw_data):
        """Handle tag changes"""
        await self.trigger_obs_event("tags_changed", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_command_result(self, parsed_data, raw_data):
        """Handle command results"""
        await self.trigger_obs_event("command_result", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_admin_command_result(self, parsed_data, raw_data):
        """Handle admin command results"""
        await self.trigger_obs_event("admin_command_result", {
            "parsed": parsed_data,
            "raw": raw_data
        })

    async def handle_bounced(self, packet):
        """Handle bounced packets"""
        await self.trigger_obs_event("packet_bounced", packet)

    async def handle_invalid_packet(self, packet):
        """Handle invalid packets"""
        await self.trigger_obs_event("invalid_packet", packet)

    async def trigger_obs_event(self, event_type: str, event_data: Dict[str, Any]):
        """Trigger OBS events based on Archipelago events"""
        if not self.obs_client:
            logger.warning("OBS client not connected, skipping event")
            return

        try:
            # Map Archipelago events to OBS actions
            obs_actions = self.config.get('obs_actions', {})

            if event_type in obs_actions:
                action_config = obs_actions[event_type]
                action_type = action_config.get('type')

                if action_type == 'scene_switch':
                    scene_name = action_config.get('scene_name')
                    self.obs_client.set_current_program_scene(scene_name)
                    logger.info(f"Switched to scene: {scene_name}")

                elif action_type == 'source_visibility':
                    source_name = action_config.get('source_name')
                    scene_name = action_config.get('scene_name')
                    visible = action_config.get('visible', True)

                    self.obs_client.set_scene_item_enabled(
                        scene_name, source_name, visible
                    )
                    logger.info(f"Set {source_name} visibility to {visible}")

                elif action_type == 'text_update':
                    source_name = action_config.get('source_name')
                    text_template = action_config.get('text_template', '')

                    # Format text with event data
                    try:
                        formatted_text = text_template.format(**event_data)
                    except (KeyError, ValueError) as e:
                        # Fallback if template formatting fails
                        formatted_text = f"{event_type}: {str(event_data)}"
                        logger.warning(f"Text template formatting failed: {e}")

                    self.obs_client.set_input_settings(
                        source_name, {"text": formatted_text}, True
                    )
                    logger.info(f"Updated text source {source_name}")

                elif action_type == 'filter_toggle':
                    source_name = action_config.get('source_name')
                    filter_name = action_config.get('filter_name')
                    enabled = action_config.get('enabled', True)

                    self.obs_client.set_source_filter_enabled(
                        source_name, filter_name, enabled
                    )
                    logger.info(f"Set filter {filter_name} on {source_name} to {enabled}")

                elif action_type == 'media_restart':
                    source_name = action_config.get('source_name')
                    self.obs_client.trigger_media_input_action(source_name, "restart")
                    logger.info(f"Restarted media source: {source_name}")

            # Log all events for debugging (can be disabled in config)
            if self.config.get('log_all_events', True):
                logger.info(f"Archipelago event: {event_type}")
                if self.config.get('log_event_data', False):
                    logger.debug(f"Event data: {event_data}")

        except Exception as e:
            logger.error(f"Failed to trigger OBS event {event_type}: {e}")

    async def listen_to_archipelago(self):
        """Main message listening loop for Archipelago"""
        try:
            async for message in self.archipelago_ws:
                try:
                    data = json.loads(message)
                    await self.handle_archipelago_message(data)
                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode message: {e}")
                except Exception as e:
                    logger.error(f"Error handling message: {e}")

        except ConnectionClosed:
            logger.warning("Archipelago connection closed")
            await self.trigger_obs_event("archipelago_disconnected", {})
        except Exception as e:
            logger.error(f"Error in message loop: {e}")

    async def run(self):
        """Main run loop"""
        logger.info("Starting Archipelago to OBS Bridge (Full Server Observer)...")

        # Connect to OBS
        if not await self.connect_obs():
            return False

        # Connect to Archipelago
        if not await self.connect_archipelago():
            return False

        self.running = True

        try:
            # Start listening to ALL Archipelago messages
            await self.listen_to_archipelago()
        except KeyboardInterrupt:
            logger.info("Received interrupt, shutting down...")
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
        finally:
            await self.cleanup()

        return True

    async def cleanup(self):
        """Clean up connections"""
        self.running = False

        if self.archipelago_ws:
            await self.archipelago_ws.close()
            logger.info("Closed Archipelago connection")

        if self.obs_client:
            self.obs_client.disconnect()
            logger.info("Closed OBS connection")


def load_config(config_file: str = 'config.json') -> Dict[str, Any]:
    """Load configuration from file"""
    default_config = {
        "archipelago_host": "localhost",
        "archipelago_port": 38281,
        "archipelago_password": "",
        "bot_name": "OBS_Observer_Bot",
        "uuid": "",
        "obs_host": "localhost",
        "obs_port": 4455,
        "obs_password": "",
        "log_all_events": True,
        "log_event_data": False,
        "obs_actions": {
            # Global item events
            "global_item_send": {
                "type": "text_update",
                "source_name": "LastItemSent",
                "text_template": "{parsed[text]}"
            },
            "global_item_found": {
                "type": "text_update",
                "source_name": "LastItemFound",
                "text_template": "{parsed[text]}"
            },
            "item_received": {
                "type": "text_update",
                "source_name": "LastItemReceived",
                "text_template": "{receiving_player} got {item_name} from {sending_player}"
            },
            "location_checked": {
                "type": "text_update",
                "source_name": "LastLocationChecked",
                "text_template": "{player_name} checked {location_name}"
            },

            # Player events
            "global_player_join": {
                "type": "text_update",
                "source_name": "PlayerStatus",
                "text_template": "Player joined: {parsed[text]}"
            },
            "global_player_part": {
                "type": "text_update",
                "source_name": "PlayerStatus",
                "text_template": "Player left: {parsed[text]}"
            },

            # Goal and major events
            "goal_completed": {
                "type": "scene_switch",
                "scene_name": "GoalCompleted"
            },
            "player_released": {
                "type": "text_update",
                "source_name": "ReleaseNotice",
                "text_template": "Player Released! {parsed[text]}"
            },
            "player_collected": {
                "type": "text_update",
                "source_name": "CollectNotice",
                "text_template": "Player Collected! {parsed[text]}"
            },

            # Connection events
            "server_connected": {
                "type": "source_visibility",
                "scene_name": "Main",
                "source_name": "ConnectedIndicator",
                "visible": True
            },
            "archipelago_disconnected": {
                "type": "source_visibility",
                "scene_name": "Main",
                "source_name": "ConnectedIndicator",
                "visible": False
            },

            # Room info
            "room_info": {
                "type": "text_update",
                "source_name": "SeedInfo",
                "text_template": "Seed: {seed_name} | Players: {players}"
            },

            # Chat
            "global_chat": {
                "type": "text_update",
                "source_name": "LastChatMessage",
                "text_template": "{parsed[text]}"
            }
        }
    }

    if os.path.exists(config_file):
        try:
            with open(config_file, 'r') as f:
                user_config = json.load(f)

                # Deep merge user config with defaults
                def deep_merge(default, user):
                    for key, value in user.items():
                        if key in default and isinstance(default[key], dict) and isinstance(value, dict):
                            deep_merge(default[key], value)
                        else:
                            default[key] = value

                deep_merge(default_config, user_config)
        except Exception as e:
            logger.warning(f"Failed to load config file {config_file}: {e}")
            logger.info("Using default configuration")
    else:
        logger.info(f"Config file {config_file} not found, using defaults")
        # Create default config file
        try:
            with open(config_file, 'w') as f:
                json.dump(default_config, f, indent=2)
            logger.info(f"Created default config file: {config_file}")
        except Exception as e:
            logger.warning(f"Failed to create config file: {e}")

    return default_config


async def main():
    """Main entry point"""
    config = load_config()
    bridge = ArchipelagoOBSBridge(config)
    await bridge.run()


if __name__ == "__main__":
    # Install required packages:
    # pip install websockets obsws-python

    asyncio.run(main())
