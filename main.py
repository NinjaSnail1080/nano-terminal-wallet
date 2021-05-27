import nanolib
import requests
import websockets

import json
import os
import asyncio
import multiprocessing
import sys
import decimal
import random
import time


def api_request(post_data, server):
    r = requests.post(server, json=post_data)
    return r.json()


def account_info(account_id, server):
    data = api_request({"action": "account_info", "account": account_id, "representative": True}, server)

    if "error" in data: # Account hasn't been opened
        return {
            "frontier": None,
            "balance": 0,
            "representative": account_id
        }
    else:
        return data


def broadcast(subtype, block, server):
    return api_request({"action": "process", "json_block": "false", "subtype": subtype, "block": block.json()}, server)


class NanoTerminalWallet():

    def __init__(self, seed, index, api_server):
        self.seed = seed
        self.index = index
        self.account_id = nanolib.generate_account_id(self.seed, self.index)
        self.api_server = api_server

        req_data = account_info(self.account_id, self.api_server)
        self.current_balance = int(req_data["balance"])
        self.representative = req_data["representative"]
        self.previous_hash = req_data["frontier"]

        self.pending = multiprocessing.Queue()


    def run(self):
        """Run the wallet application"""

        websocket_process = multiprocessing.Process(target=self.run_handle_websocket)
        websocket_process.start()

        auto_receive_process = multiprocessing.Process(target=self.auto_receive)
        auto_receive_process.start()

        self.wallet()

        websocket_process.kill()
        auto_receive_process.kill()
        print("\nClosed\n")


    def wallet(self):
        """Main function for the wallet. Handles the UI and the main loop"""

        while True:
            print(
                f"""
                Current Account Balance: {nanolib.units.convert(self.current_balance, "raw", "Mnano")}
                {self.account_id}
                """
            )
            cmd = input("Send - \"s\"   Change Rep - \"c\"    Reload - Enter    Quit - \"q\"\n")
            if cmd.lower() == "s":
                to = input("\nTo Address: ")
                amount = input("Amount: ")
                amount = int(nanolib.units.convert(decimal.Decimal(amount), "Mnano", "raw"))
                self.current_balance -= amount

                new_block = nanolib.Block(
                    block_type="state",
                    account=self.account_id,
                    representative=self.representative,
                    previous=self.previous_hash,
                    balance=self.current_balance,
                    link_as_account=to
                )
                try:
                    work_request = api_request({"action": "work_generate", "hash": new_block.work_block_hash}, self.api_server)
                    new_block.set_work(work_request["work"])
                except:
                    new_block.solve_work()
                new_block.sign(nanolib.generate_account_private_key(self.seed, self.index))
                process_request = broadcast("send", new_block, self.api_server)

                if "hash" in process_request:
                    self.previous_hash = process_request["hash"]
                    message = f"Sent {nanolib.units.convert(amount, 'raw', 'Mnano')} Nano"
                    print(f"\n{'-'*len(message)}\n{message}\n{'-'*len(message)}\n")
                else:
                    self.current_balance = int(account_info(self.account_id, self.api_server)["balance"])

            elif cmd.lower() == "c":
                rep = input("\nNew Representative: ")

                new_block = nanolib.Block(
                    block_type="state",
                    account=self.account_id,
                    representative=rep,
                    previous=self.previous_hash,
                    balance=self.current_balance,
                    link="0"*64
                )
                try:
                    work_request = api_request({"action": "work_generate", "hash": new_block.work_block_hash}, self.api_server)
                    new_block.set_work(work_request["work"])
                except:
                    new_block.solve_work()
                new_block.sign(nanolib.generate_account_private_key(self.seed, self.index))
                process_request = broadcast("change", new_block, self.api_server)

                if "hash" in process_request:
                    self.previous_hash = process_request["hash"]
                    self.representative = new_block.representative
                    print("\n----------------------\nChanged Representative\n----------------------\n")

            elif cmd.lower() == "q":
                print("\nQuitting...\n")
                break
            else:
                self.current_balance = int(account_info(self.account_id, self.api_server)["balance"])
                continue


    async def handle_websocket(self):
        """Subscribes to a websocket and receives new transactions involving your account"""

        while True:
            try:
                async with websockets.connect("wss://ws.mynano.ninja") as websocket:

                    message = {
                        "action": "subscribe",
                        "topic": "confirmation",
                        "ack": True,
                        "options": {
                            "accounts": [self.account_id]
                        }
                    }

                    await websocket.send(json.dumps(message))

                    while True:
                        rec = json.loads(await websocket.recv())
                        topic = rec.get("topic", None)
                        if topic:
                            message = rec["message"]
                            if topic == "confirmation":
                                block = rec["message"]["block"]
                                if block["subtype"] == "send":
                                    self.pending.put_nowait(rec["message"])
            except:
                continue


    def run_handle_websocket(self):
        """Runs the handle_websocket function"""

        asyncio.run(self.handle_websocket())


    def auto_receive(self):
        """Automatically broadcasts receive blocks for incoming transactions. Also handles pending transactions"""

        pending_block_hashes = api_request({"action": "pending", "account": self.account_id}, self.api_server)["blocks"]
        if pending_block_hashes:
            pending_blocks = api_request({"action": "blocks_info", "json_block": "true", "hashes": pending_block_hashes}, self.api_server)["blocks"]
            for _hash, _info in pending_blocks.items():
                if _info["subtype"] == "send":
                    _info.update({"hash": _hash})
                    self.pending.put_nowait(_info)

        while True:
            if not self.pending.empty():
                pending_block = self.pending.get_nowait()
                self.current_balance += int(pending_block["amount"])
                new_block = nanolib.Block(
                    block_type="state",
                    account=self.account_id,
                    representative=self.representative,
                    previous=self.previous_hash,
                    balance=self.current_balance,
                    link=pending_block["hash"]
                )
                try:
                    work_request = api_request({"action": "work_generate", "hash": new_block.work_block_hash}, self.api_server)
                    new_block.set_work(work_request["work"])
                except:
                    new_block.solve_work()
                new_block.sign(nanolib.generate_account_private_key(self.seed, self.index))
                process_request = broadcast("receive", new_block, self.api_server)

                if "hash" in process_request:
                    self.previous_hash = process_request["hash"]
                    message = f"Recieved {nanolib.units.convert(int(pending_block['amount']), 'raw', 'Mnano')} Nano    (Press Enter to reload)"
                    print(f"\n{'-'*len(message)}\n{message}\n{'-'*len(message)}\n")
                else:
                    self.current_balance = int(account_info(self.account_id, self.api_server)["balance"])

            time.sleep(0.1) # To prevent excess CPU usage


if __name__ == "__main__":
    DIRECTORY = os.path.dirname(__file__)

    with open(os.path.join(DIRECTORY, "seed.txt")) as f:
        SEED = f.readline()

    with open(os.path.join(DIRECTORY, "api_servers.txt")) as f:
        API_SERVERS_LIST = f.read()
        API_SERVERS_LIST = API_SERVERS_LIST.split("\n")
        API_SERVERS_LIST = list(filter(None, API_SERVERS_LIST))

    API_SERVER = random.choice(API_SERVERS_LIST)

    wallet = NanoTerminalWallet(SEED, 0, API_SERVER)

    wallet.run()

    sys.exit()
