import asyncio
import discord
import os
import aiohttp
import requests
import logging
import json
from twitchio.ext import commands
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Discord bot token and Twitch API credentials from environment variables
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET')
TWITCH_OAUTH_TOKEN = os.getenv('TWITCH_OAUTH_TOKEN')
TWITCH_NICKNAME = os.getenv('TWITCH_NICKNAME')  # Your bot's or your Twitch username

# Load the Discord channel IDs from environment variables
TRACK_CHANNEL_ID = os.getenv('TRACK_CHANNEL_ID')
OUTPUT_CHANNEL_ID = os.getenv('OUTPUT_CHANNEL_ID')

# Log environment variable values (ensure no sensitive info is logged in production)
logger.info(f'DISCORD_TOKEN: {DISCORD_TOKEN}')
logger.info(f'TWITCH_CLIENT_ID: {TWITCH_CLIENT_ID}')
logger.info(f'TWITCH_CLIENT_SECRET: {TWITCH_CLIENT_SECRET}')
logger.info(f'TRACK_CHANNEL_ID: {TRACK_CHANNEL_ID}')
logger.info(f'OUTPUT_CHANNEL_ID: {OUTPUT_CHANNEL_ID}')

# Ensure channel IDs are set and convert them to integers
if TRACK_CHANNEL_ID is None or OUTPUT_CHANNEL_ID is None:
    logger.error("Channel IDs are not set correctly in the environment variables.")
    raise ValueError("Channel IDs are required.")

TRACK_CHANNEL_ID = int(TRACK_CHANNEL_ID)
OUTPUT_CHANNEL_ID = int(OUTPUT_CHANNEL_ID)

# URL to the bot_usernames.json file in your GitHub repository
bot_usernames_url = 'https://raw.githubusercontent.com/HZRD-Media/Twitch-Viewer-Tracker-Discord-Bot/main/bot_usernames.json'

# Async function to download and load the JSON file
async def load_bot_usernames():
    async with aiohttp.ClientSession() as session:
        async with session.get(bot_usernames_url) as response:
            if response.status == 200:
                text = await response.text()  # Get the response as text
                try:
                    data = json.loads(text)  # Parse the text as JSON
                    logger.info(f"Bots to ignore loaded successfully.")  # More user-friendly log message
                    return data.get('bot_usernames', [])
                except json.JSONDecodeError as e:
                    logger.error(f"JSON decode error: {e}")
                    return []
            else:
                logger.error(f"Failed to download bot_usernames.json, status code: {response.status}")
                return []

# Ensure there's a current event loop in the main thread
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

# Load the bot usernames using the event loop
bot_usernames = loop.run_until_complete(load_bot_usernames())

# Initialize the Discord client
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True  # To track deleted messages
client = discord.Client(intents=intents)

# Store active Twitch links and associated tasks
active_links = {}

# Store user appearances across multiple lists
user_appearance_count = {}

# Store raiders
raiders = set()

# Initialize TwitchIO Bot
class TwitchBot(commands.Bot):

    def __init__(self, logger):
        super().__init__(token=TWITCH_OAUTH_TOKEN, prefix='!', initial_channels=[])
        self.active_users = set()  # Store active users who have sent messages
        self.logger = logger  # Store the logger instance

    async def _connect(self):
        # Add required capabilities to detect events like raids
        self._connection.capabilities.append('twitch.tv/tags')
        self._connection.capabilities.append('twitch.tv/commands')
        self._connection.capabilities.append('twitch.tv/membership')
        await super()._connect()

    async def event_ready(self):
        self.logger.info(f'Logged in to Twitch as {self.nick}')

    async def event_message(self, message):
        if message.echo:
            return
        self.active_users.add(message.author.name)
        self.logger.info(f'Detected chat user: {message.author.name}')
        await self.handle_commands(message)

    async def event_usernotice(self, channel, tags):
        """
        Listen for user notices like raids. `tags` contain metadata about the event.
        """
        self.logger.debug(f"Received USERNOTICE event in {channel.name}: {tags}")
        
        # Log all tags to ensure we're capturing the correct event data
        for key, value in tags.items():
            self.logger.info(f"Tag: {key} => {value}")
        
        if tags.get('msg-id') == 'raid':
            raider = tags.get('display-name')
            viewers = tags.get('msg-param-viewerCount')
            self.logger.info(f"Raid detected! {raider} is raiding with {viewers} viewers.")
            raiders.add(raider)  # Add raider to the raiders set

            # Send a message to the Discord output channel
            output_channel = client.get_channel(OUTPUT_CHANNEL_ID)
            if output_channel:
                try:
                    await output_channel.send(f"Thanks {raider} for the raid with {viewers} viewers!")
                except discord.errors.HTTPException as e:
                    self.logger.error(f"Failed to send message: {e}")
            else:
                self.logger.warning("Failed to find the output channel to send a raid message.")

    async def event_raw_data(self, data):
        """Logs raw data to track different types of messages."""
        self.logger.debug(f"Raw data event received: {data}")

    async def get_active_users(self):
        return list(self.active_users)

    async def _join_channel(self, channel):
        try:
            await super()._join_channel(channel)
        except ConnectionResetError as e:
            self.logger.error(f"ConnectionResetError while joining channel {channel}: {e}")
            await self.reconnect()

    async def reconnect(self, attempt=1):
        try:
            self.logger.info(f"Attempting to reconnect... (Attempt {attempt})")
            await self.close()  # Close existing connection
            await self.connect()  # Attempt to reconnect
        except Exception as e:
            self.logger.error(f"Failed to reconnect: {e}")
            await asyncio.sleep(min(10 * attempt, 60))  # Exponential backoff
            await self.reconnect(attempt + 1)

# Initialize the TwitchBot with the logger instance
twitch_bot = TwitchBot(logger=logger)

# Function to post active viewers list in Discord
async def post_viewers_list(twitch_username):
    output_channel = client.get_channel(OUTPUT_CHANNEL_ID)  # Get the output channel
    while twitch_username in active_links:
        try:
            active_users = await twitch_bot.get_active_users()
            if active_users:
                # Filter out bot usernames using the list from the JSON file
                filtered_users = [user for user in active_users if user.lower() not in bot_usernames]
                
                if filtered_users:
                    viewers_list = ', '.join(filtered_users)
                    try:
                        await output_channel.send(f'Active users interacting in {twitch_username}: {viewers_list}')
                    except discord.errors.HTTPException as e:
                        logger.error(f"Failed to send message: {e}")
                    
                    # Track user appearances
                    for user in filtered_users:
                        user_appearance_count[user] = user_appearance_count.get(user, 0) + 1
                else:
                    try:
                        await output_channel.send(f'No non-bot chat users detected for {twitch_username}.')
                    except discord.errors.HTTPException as e:
                        logger.error(f"Failed to send message: {e}")

            else:
                try:
                    await output_channel.send(f'No active chat users detected for {twitch_username}.')
                except discord.errors.HTTPException as e:
                    logger.error(f"Failed to send message: {e}")
        except ConnectionResetError as e:
            logger.error(f"Connection reset error for {twitch_username}: {e}")
            await twitch_bot.reconnect()

        try:
            stream_data = await get_twitch_stream_data(twitch_username)
            if stream_data:
                viewer_count = stream_data['viewer_count']
                try:
                    await output_channel.send(f'{twitch_username} currently has {viewer_count} viewers.')
                except discord.errors.HTTPException as e:
                    logger.error(f"Failed to send message: {e}")
            else:
                try:
                    await output_channel.send(f'{twitch_username} is not currently live.')
                except discord.errors.HTTPException as e:
                    logger.error(f"Failed to send message: {e}")
        except ConnectionResetError as e:
            logger.error(f"Connection reset error while fetching stream data for {twitch_username}: {e}")
            await twitch_bot.reconnect()

        twitch_bot.active_users.clear()
        await asyncio.sleep(1200)

    logger.info(f"Tracking for {twitch_username} has been stopped.")

# Function to start tracking a Twitch channel
async def start_tracking(twitch_username):
    output_channel = client.get_channel(OUTPUT_CHANNEL_ID)  # Get the output channel
    # Check if we're already tracking this username to prevent duplicate tasks
    if twitch_username not in active_links:
        task = asyncio.create_task(post_viewers_list(twitch_username))
        active_links[twitch_username] = task
        try:
            await output_channel.send(f'Started tracking {twitch_username}.')
        except discord.errors.HTTPException as e:
            logger.error(f"Failed to send message: {e}")
        await twitch_bot.join_channels([twitch_username])

# Event: Bot is ready
@client.event
async def on_ready():
    logger.info(f'We have logged in as {client.user}')
    output_channel = client.get_channel(OUTPUT_CHANNEL_ID)
    if output_channel:
        await output_channel.send("Bot is online and ready!")
    else:
        logger.error("Output channel not found.")

# Event: Message received
@client.event
async def on_message(message):
    logger.info(f"Message received in channel {message.channel.id}: {message.content}")
    
    if message.author == client.user:
        logger.info("Ignoring the bot's own message.")
        return

    if message.channel.id == TRACK_CHANNEL_ID:
        logger.info(f"Message is in the correct channel: {TRACK_CHANNEL_ID}")
        
        if 'twitch.tv' in message.content:
            logger.info("Twitch link detected")
            
            # Extract the Twitch username from the link
            twitch_username = message.content.split('twitch.tv/')[-1].split()[0]
            logger.info(f"Extracted Twitch username: {twitch_username}")

            if twitch_username not in active_links:
                logger.info(f"Starting tracking for {twitch_username}")
                await start_tracking(twitch_username)
    else:
        logger.info("Message received in a different channel.")

# Event: Message deleted
@client.event
async def on_message_delete(message):
    output_channel = client.get_channel(OUTPUT_CHANNEL_ID)  # Get the output channel

    # Only consider deletions in the specified channel
    if message.channel.id == TRACK_CHANNEL_ID:
        if 'twitch.tv' in message.content:
            # Extract the Twitch username from the link
            twitch_username = message.content.split('twitch.tv/')[-1].split()[0]

            # Stop tracking if the link is removed
            if twitch_username in active_links:
                # Cancel the ongoing task
                task = active_links.pop(twitch_username, None)
                if task:
                    task.cancel()
                    try:
                        await output_channel.send(f'Stopped tracking {twitch_username} as the link was removed.')
                    except discord.errors.HTTPException as e:
                        logger.error(f"Failed to send message: {e}")

                    # Leave the Twitch channel
                    await twitch_bot.part_channels([twitch_username])
                    logger.info(f"Stopped tracking {twitch_username} and left the channel.")

                    # Clear the console after stopping tracking
                    os.system('cls' if os.name == 'nt' else 'clear')

                    # Reload bot usernames from JSON file
                    global bot_usernames
                    bot_usernames = await load_bot_usernames()
                    logger.info(f"Reloaded bot usernames: {bot_usernames}")

                # Find and display users who appeared in more than one list
                multi_appearance_users = [user for user, count in user_appearance_count.items() if count > 1]
                single_appearance_users = [user for user, count in user_appearance_count.items() if count == 1]

                # Print the single appearance list first
                if single_appearance_users:
                    single_user_list = ', '.join(reversed(single_appearance_users))
                    try:
                        await output_channel.send(f'Users who appeared in only one list: {single_user_list}')
                    except discord.errors.HTTPException as e:
                        logger.error(f"Failed to send message: {e}")
                else:
                    try:
                        await output_channel.send('No users appeared in only one list.')
                    except discord.errors.HTTPException as e:
                        logger.error(f"Failed to send message: {e}")

                # Then print the multiple appearance list
                if multi_appearance_users:
                    multi_user_list = ', '.join(reversed(multi_appearance_users))
                    try:
                        await output_channel.send(f'Users who appeared in multiple lists: {multi_user_list}')
                    except discord.errors.HTTPException as e:
                        logger.error(f"Failed to send message: {e}")
                else:
                    try:
                        await output_channel.send('No users appeared in more than one list.')
                    except discord.errors.HTTPException as e:
                        logger.error(f"Failed to send message: {e}")

                # Display raiders list
                if raiders:
                    raiders_list = ', '.join(raiders)
                    try:
                        await output_channel.send(f'Users who raided the channel: {raiders_list}')
                    except discord.errors.HTTPException as e:
                        logger.error(f"Failed to send message: {e}")
                else:
                    try:
                        await output_channel.send('No users raided the channel.')
                    except discord.errors.HTTPException as e:
                        logger.error(f"Failed to send message: {e}")

                # Clear the user appearance count and raiders list for future tracking
                user_appearance_count.clear()
                raiders.clear()

# Function to get Twitch stream data
async def get_twitch_stream_data(username):
    url = 'https://api.twitch.tv/helix/streams'
    headers = {
        'Client-ID': TWITCH_CLIENT_ID,
        'Authorization': f'Bearer {await get_twitch_oauth_token()}'
    }
    params = {
        'user_login': username
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                if data['data']:
                    return data['data'][0]
                else:
                    return None
    except aiohttp.ClientError as err:
        logger.error(f"An error occurred while fetching stream data: {err}")
        return None

# Function to get Twitch OAuth token
async def get_twitch_oauth_token():
    url = 'https://id.twitch.tv/oauth2/token'
    params = {
        'client_id': TWITCH_CLIENT_ID,
        'client_secret': TWITCH_CLIENT_SECRET,
        'grant_type': 'client_credentials'
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                return data['access_token']
    except aiohttp.ClientError as err:
        logger.error(f"An error occurred while obtaining OAuth token: {err}")
        return None

# Run both the Discord and Twitch bots
loop.create_task(client.start(DISCORD_TOKEN))
loop.create_task(twitch_bot.start())
loop.run_forever()
