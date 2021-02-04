import importlib
import json
import asyncio
import concurrent.futures
from copy import deepcopy
import logging
import logstash
import pandas as pd

import data
from data.helpers import get_standard_battle_sets
from data.helpers import get_most_likely_ability
from data.helpers import get_most_likely_item
import constants
import config
from showdown.engine.evaluate import Scoring
from showdown.battle import Pokemon
from showdown.battle import LastUsedMove
from showdown.battle_modifier import async_update_battle, turn

from showdown.websocket_client import PSWebsocketClient

logger = logging.getLogger('python-logstash-logger')
host = '10.100.0.30'
logger.addHandler(logstash.TCPLogstashHandler(host, 5000, version=1))

opponent_data = {}
opponent_data["battle"] = []
opponent_data["pokemon"] = []




def battle_is_finished(battle_tag, msg):
    return msg.startswith(">{}".format(battle_tag)) and constants.WIN_STRING in msg and constants.CHAT_STRING not in msg


async def async_pick_move(battle):
    battle_copy = deepcopy(battle)
    if battle_copy.request_json:
        battle_copy.user.from_json(battle_copy.request_json)

    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        best_move = await loop.run_in_executor(
            pool, battle_copy.find_best_move
        )
    choice = best_move[0]
    if constants.SWITCH_STRING in choice:
        battle.user.last_used_move = LastUsedMove(battle.user.active.name, "switch {}".format(choice.split()[-1]), battle.turn)
    else:
        battle.user.last_used_move = LastUsedMove(battle.user.active.name, choice.split()[2], battle.turn)
    return best_move


async def handle_team_preview(battle, ps_websocket_client):
    battle_copy = deepcopy(battle)
    battle_copy.user.active = Pokemon.get_dummy()
    battle_copy.opponent.active = Pokemon.get_dummy()

    best_move = await async_pick_move(battle_copy)
    size_of_team = len(battle.user.reserve) + 1
    team_list_indexes = list(range(1, size_of_team))
    choice_digit = int(best_move[0].split()[-1])

    team_list_indexes.remove(choice_digit)
    message = ["/team {}{}|{}".format(choice_digit, "".join(str(x) for x in team_list_indexes), battle.rqid)]
    battle.user.active = battle.user.reserve.pop(choice_digit - 1)

    await ps_websocket_client.send_message(battle.battle_tag, message)


async def get_battle_tag_and_opponent(ps_websocket_client: PSWebsocketClient):
    while True:
        msg = await ps_websocket_client.receive_message()
        split_msg = msg.split('|')
        first_msg = split_msg[0]
        if 'battle' in first_msg:
            battle_tag = first_msg.replace('>', '').strip()
            user_name = split_msg[-1].replace('☆', '').strip()
            opponent_name = split_msg[4].replace(user_name, '').replace('vs.', '').strip()
            return battle_tag, opponent_name


async def initialize_battle_with_tag(ps_websocket_client: PSWebsocketClient, set_request_json=True):
    battle_module = importlib.import_module('showdown.battle_bots.{}.main'.format(config.battle_bot_module))

    battle_tag, opponent_name = await get_battle_tag_and_opponent(ps_websocket_client)
    while True:
        msg = await ps_websocket_client.receive_message()
        split_msg = msg.split('|')
        if split_msg[1].strip() == 'request' and split_msg[2].strip():
            user_json = json.loads(split_msg[2].strip('\''))
            user_id = user_json[constants.SIDE][constants.ID]
            opponent_id = constants.ID_LOOKUP[user_id]
            battle = battle_module.BattleBot(battle_tag)
            battle.opponent.name = opponent_id
            battle.opponent.account_name = opponent_name

            if set_request_json:
                battle.request_json = user_json

            return battle, opponent_id, user_json


async def read_messages_until_first_pokemon_is_seen(ps_websocket_client, battle, opponent_id, user_json):
    # keep reading messages until the opponent's first pokemon is seen
    # this is run when starting non team-preview battles
    while True:
        msg = await ps_websocket_client.receive_message()
        if constants.START_STRING in msg:
            split_msg = msg.split(constants.START_STRING)[-1].split('\n')
            for line in split_msg:
                if opponent_id in line and constants.SWITCH_STRING in line:
                    battle.start_non_team_preview_battle(user_json, line)

                elif battle.started:
                    await async_update_battle(battle, line)

            # first move needs to be picked here
            best_move = await async_pick_move(battle)
            await ps_websocket_client.send_message(battle.battle_tag, best_move)

            return


async def start_random_battle(ps_websocket_client: PSWebsocketClient, pokemon_battle_type):
    battle, opponent_id, user_json = await initialize_battle_with_tag(ps_websocket_client)
    battle.battle_type = constants.RANDOM_BATTLE
    battle.generation = pokemon_battle_type[:4]

    await read_messages_until_first_pokemon_is_seen(ps_websocket_client, battle, opponent_id, user_json)

    return battle


async def start_standard_battle(ps_websocket_client: PSWebsocketClient, pokemon_battle_type):
    battle, opponent_id, user_json = await initialize_battle_with_tag(ps_websocket_client, set_request_json=False)
    battle.battle_type = constants.STANDARD_BATTLE
    battle.generation = pokemon_battle_type[:4]

    smogon_usage_data = get_standard_battle_sets(pokemon_battle_type)
    data.pokemon_sets = smogon_usage_data

    if battle.generation in constants.NO_TEAM_PREVIEW_GENS:
        await read_messages_until_first_pokemon_is_seen(ps_websocket_client, battle, opponent_id, user_json)
    else:
        msg = ''
        while constants.START_TEAM_PREVIEW not in msg:
            msg = await ps_websocket_client.receive_message()

        preview_string_lines = msg.split(constants.START_TEAM_PREVIEW)[-1].split('\n')

        opponent_pokemon = []
        for line in preview_string_lines:
            if not line:
                continue

            split_line = line.split('|')
            if split_line[1] == constants.TEAM_PREVIEW_POKE and split_line[2].strip() == opponent_id:
                opponent_pokemon.append(split_line[3])

        battle.initialize_team_preview(user_json, opponent_pokemon)
        await handle_team_preview(battle, ps_websocket_client)

    return battle


async def start_battle(ps_websocket_client, pokemon_battle_type):
    if "random" in pokemon_battle_type:
        Scoring.POKEMON_ALIVE_STATIC = 30  # random battle benefits from a lower static score for an alive pkmn
        battle = await start_random_battle(ps_websocket_client, pokemon_battle_type)
    else:
        battle = await start_standard_battle(ps_websocket_client, pokemon_battle_type)

    await ps_websocket_client.send_message(battle.battle_tag, [config.greeting_message])
    await ps_websocket_client.send_message(battle.battle_tag, ['/timer on'])

    return battle

bid = 0
async def pokemon_battle(ps_websocket_client, pokemon_battle_type):
    global bid
    print("Starting new battle...")
    battle = await start_battle(ps_websocket_client, pokemon_battle_type)
    while True:
        msg = await ps_websocket_client.receive_message()
        if battle_is_finished(battle.battle_tag, msg):
            winner = msg.split(constants.WIN_STRING)[-1].split('\n')[0].strip()
            ##
            # opponent_json = json.dumps(battle.opponent)
            # logger.info("OPPONENT_JSON: {}".format(battle.opponent.from_json))

            if battle.opponent.active is not None:
                opponent_data["battle"].append({
                    "bid": bid,
                    "player": battle.opponent.account_name,
                    "type": pokemon_battle_type,
                    "turns": battle.turn
                })
                opponent_data["pokemon"].append({
                    "name": battle.opponent.active.name,
                    "ability": battle.opponent.active.ability,
                    "types": battle.opponent.active.types,
                    "item": battle.opponent.active.item,
                    "moves":str(battle.opponent.active.moves).strip("[ "+" ]").split(", ")
                })
                for pokemon in battle.opponent.reserve:
                    opponent_data["pokemon"].append({
                        "name": pokemon.name,
                        "ability": pokemon.ability,
                        "types": pokemon.types,
                        "item": pokemon.item,
                        # "moves": pokemon.moves
                        "moves": str(pokemon.moves).strip("[ "+" ]").split(", ")
                    })


                for x in opponent_data["pokemon"]:
                    if x["ability"] == None:
                        # x["ability"] = "NULL"
                        x["ability"] = get_most_likely_ability(x["name"])
                    if x["item"] == "unknown_item" or x["item"] is None:
                        x["item"] = get_most_likely_item(x["name"])
                    if winner == battle.opponent.account_name:
                        # x["win"] = "True"
                        x["win"] = 1.0
                    else:
                        # x["win"] = "False"
                        x["win"] = 0.0

                while len(opponent_data["pokemon"]) < 6:
                    opponent_data["pokemon"].append({
                        "name": " ",
                        "ability": " ",
                        "types": " ",
                        "item": " ",
                        "moves": " "
                    })
                # for x in opponent_data["pokemon"]:
                    # logger.info("MOVES: {}".format(', '.join(x["moves"])))
                    # logger.info("MOVES: {}".format(str(x["moves"]).strip("["+'"'+"]").split(",")))        
                logger.info("{}".format(json.dumps(opponent_data)))

                opponent_data["battle"].clear()
                opponent_data["pokemon"].clear()
                bid += 1


            ####
            #logger.debug("Winner: {}".format(winner))
            await ps_websocket_client.send_message(battle.battle_tag, [config.battle_ending_message])
            await ps_websocket_client.leave_battle(battle.battle_tag, save_replay=config.save_replay)
            return winner
        else:
            action_required = await async_update_battle(battle, msg)
            # logger.info("{}".format(battle.user.from_json))
            if action_required and not battle.wait:
                best_move = await async_pick_move(battle)
                await ps_websocket_client.send_message(battle.battle_tag, best_move)
