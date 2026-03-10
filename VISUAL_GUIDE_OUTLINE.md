# Pollen Visual Guide — Content Outline

This document is the technical content outline for an illustrated, mobile-friendly guide to Pollen and Petals. It is structured as a series of pages, each with the text content and notes for the illustrator describing what to draw.

**Format:** Mobile-first vertical scroll. Each page is one screen on a phone. Large illustrations, minimal text, big tap targets.

**Tone:** Friendly, non-technical, inviting. Like explaining it to a smart friend who doesn't work in tech.

**Color palette suggestion:** Dark background (matching the Pollen UI), with bright accent colors for the network nodes (greens, blues, purples). Warm amber/gold for highlights.

---

## Page 1: Cover

### Text

**Pollen**
*Run AI on a swarm of GPUs*

A simple guide to decentralized AI chat.

### Illustrator notes

Full-bleed dark background. The word "Pollen" in large, clean type. Below it, an illustration of glowing dots (nodes) connected by thin lines forming a loose organic network shape — like a constellation or a dandelion puff. Some dots are larger (they have GPUs), some smaller. The whole thing should feel alive and decentralized — no center, no hierarchy. Subtle particle/pollen-grain motifs floating around the edges.

---

## Page 2: What is Petals?

### Text

**What is Petals?**

Big AI models are too large for one computer. GPT-4 scale models need hundreds of gigabytes of memory.

Petals solves this by splitting the model into pieces and spreading them across many computers. Each computer runs a small chunk. Together, they act like one giant machine.

Think of it like a relay race: your question passes from computer to computer, each one doing a little bit of the work, until the full answer comes out the other end.

### Illustrator notes

**Top panel:** Show a single large server/computer looking overwhelmed — smoke coming out, red warning lights, a thought bubble showing a massive brain that doesn't fit inside it. Label: "One computer can't run the whole model."

**Bottom panel:** Show the same brain split into colorful slices (like a layer cake or a sliced loaf of bread), with each slice sitting on a different small computer. The computers are connected by glowing lines. Each computer looks happy/relaxed. Small labels on each slice: "Layer 1-4", "Layer 5-8", "Layer 9-12", etc. Label: "But many computers can share the work."

Between the panels, a simple arrow or transition showing the transformation from one to many.

---

## Page 3: What is Pollen?

### Text

**What is Pollen?**

Pollen is the part you actually use. It's the chat screen, the bots, the dashboard — the friendly face on top of the Petals network.

When you type a message in Pollen, here's what happens:

1. Your message goes to the Pollen server
2. Pollen sends it into the Petals network
3. Your message travels through the chain of computers
4. Each one adds a little more to the answer
5. The words stream back to your screen in real-time

You see a smooth typing effect. Behind the scenes, a swarm of GPUs across the internet is collaborating to write each word.

### Illustrator notes

Show a phone screen with the Pollen chat UI (dark theme, message bubbles). An arrow leads from the phone to a small server icon labeled "Pollen", then the arrow fans out into a chain of 4-5 computer/GPU icons in a line, each one glowing as the data passes through it (like a lit fuse or a domino chain), then the arrow returns back to the phone.

Animate this as a sequence if the format supports it, or show it as a left-to-right flow diagram. The key visual metaphor: one message goes in, travels through a chain, answer comes back.

Small inset showing the phone screen with words appearing one by one (streaming effect) — maybe 3 frames showing progressively more text.

---

## Page 4: What you need

### Text

**What you need**

To run a Pollen node, you need:

**A computer with a GPU**
Any NVIDIA GPU with 8 GB or more of memory works. Gaming GPUs (RTX 3060 and up) are fine. The more memory, the more of the model you can run.

**An internet connection**
Your computer needs to be reachable from the internet on a few specific ports. Cloud servers work out of the box. Home servers need a little extra setup.

**Ubuntu Linux**
Version 20.04 or newer. Other Linux distributions work too, but the install script is tested on Ubuntu.

**15 minutes**
That's how long setup takes. Seriously.

### Illustrator notes

Four items in a 2x2 grid, each with a simple icon:

1. **GPU card** — illustration of a graphics card with a glowing chip. Small label: "NVIDIA, 8 GB+" Maybe show a few example cards: a gaming RTX card and a data center Tesla card.

2. **Internet** — a globe with signal waves or connection lines radiating out. Show an arrow going both ways (in and out) to emphasize that inbound connections matter.

3. **Ubuntu** — the Ubuntu logo (circle of friends) or a simple terminal window with a command prompt.

4. **Clock** — a simple clock face or timer showing 15 minutes. Keep it playful.

---

## Page 5: How the network works

### Text

**How the network works**

Every big AI model is made of layers — usually 32 to 80 of them, stacked on top of each other.

In Petals, different computers each take responsibility for a few layers. We call each layer a "block."

When you ask the AI a question, your message enters at block 0 and works its way through every block in order. Each computer processes its blocks and passes the result to the next one.

The answer builds up token by token (roughly one word at a time). As soon as each token finishes its journey through all the blocks, it appears on your screen.

**What's the DHT?**

Every computer announces which blocks it has to a shared directory called the DHT (distributed hash table). Think of it as a bulletin board where everyone posts "I have blocks 8 through 15" and anyone can look up who has what.

### Illustrator notes

**Main illustration:** A horizontal pipeline diagram showing blocks 0 through 31 (simplified — show maybe 8 blocks, label them 0-3, 4-7, 8-15, 16-31 to show grouping). Each group of blocks sits inside a colored box representing a different computer/peer. Data (shown as a glowing orb or particle) enters from the left and travels rightward through each box.

Below the pipeline, show a small "DHT" board — visualized as a cork bulletin board or a shared notebook — with small cards pinned to it saying things like "Peer A: blocks 0-3", "Peer B: blocks 4-7", etc.

**Small inset:** Show what happens when a new computer joins — it picks uncovered blocks, announces them on the DHT board, and fills in a gap in the pipeline. Visualize the gap closing.

---

## Page 6: Setting it up — overview

### Text

**Setting it up**

Here's the whole process at a glance:

1. **Get a server** — cloud or home machine with a GPU
2. **Install GPU drivers** — so your computer can talk to the GPU
3. **Open your firewall** — so other computers can reach you
4. **Run the installer** — one command does the rest
5. **Enter your config** — choose a model, connect to peers
6. **Done** — open the chat in your browser

The next few pages walk through each step. If you just want to get going fast, here's the speed run:

```
ssh into your server
git clone https://github.com/nalamk/pollen.git
cd pollen
sudo ./install.sh
```

### Illustrator notes

A vertical numbered list (1 through 6), each item with a small icon to its left. Draw it like a roadmap or a trail — a dotted path connecting each step from top to bottom, with each step as a waypoint.

Icons for each step:
1. Server/computer icon
2. GPU chip with a checkmark
3. A wall (firewall) with a door opening
4. A terminal with a progress bar
5. A form/checklist with a pencil
6. A browser window with a chat bubble (the finish line)

At the bottom, the speed-run commands in a terminal window illustration (dark background, green or white monospace text).

---

## Page 7: Setting it up — GPU drivers

### Text

**Install GPU drivers**

If you're using a cloud server (AWS, GCP, Lambda Labs), drivers are usually pre-installed. Test by typing:

```
nvidia-smi
```

If you see your GPU listed, skip this step.

If not, install drivers with:

```
sudo apt update
sudo apt install nvidia-driver-535
sudo reboot
```

After reboot, run `nvidia-smi` again. You should see your GPU model, how much memory it has, and the driver version.

### Illustrator notes

A terminal window showing the `nvidia-smi` output — but simplified and stylized. Don't show the full real output (it's ugly). Instead, show a clean version with the key info highlighted:

- GPU name: "Tesla T4" (highlighted in green)
- Memory: "15360 MiB" (highlighted in blue)
- Driver: "535.xx" (highlighted in amber)

Below the terminal, a small "success" indicator — a green checkmark with "GPU ready".

If the command fails, show a red X with an arrow pointing to the install command.

---

## Page 8: Setting it up — firewall

### Text

**Open your firewall**

Other Petals computers need to reach your server on port 31337. If you want people to use the web chat, port 5000 also needs to be open.

**Cloud servers (AWS, GCP):** Go to your security group settings and add rules to allow TCP traffic on ports 31337, 31338, and 5000 from anywhere (0.0.0.0/0).

**Home servers:** Log into your router and set up port forwarding for those three ports to your server's local IP address.

**Quick test:** From any other computer, run:

```
nc -zv your-server-ip 31337
```

If it says "succeeded" or "open", you're good.

### Illustrator notes

Show a simplified network diagram:

**Cloud version (top half):** A cloud shape containing a server icon. Outside the cloud, arrows coming in labeled "Port 31337" and "Port 5000". A shield/security-group icon with a green checkmark next to the open ports.

**Home version (bottom half):** A house containing a server icon and a router icon. The router has arrows from the internet coming in, forwarding through to the server. Label the ports on the arrows. Show the router's admin page as a tiny inset with port forwarding rules.

Between the two scenarios, a "Which one are you?" fork in the path.

---

## Page 9: Joining a cluster

### Text

**Joining a cluster**

If someone else is already running Pollen, joining their network is easy.

Ask them for their **peer address**. It's a long string that looks like this:

```
/ip4/54.166.153.249/tcp/31337/p2p/QmXxxYyyZzz
```

During installation, paste it when the script asks for "DHT initial peers."

That's it. Your computer will connect to theirs, download the model blocks it needs to serve, and start contributing to the network.

**Starting a new cluster?**

Just skip the peers question. Your node becomes the first peer. Share your peer address with others so they can join you.

### Illustrator notes

**Top:** Show two servers connected by a line. One is labeled "Existing cluster" with a speech bubble showing the peer address string. An arrow points from it to the second server labeled "You", which has a terminal window where the address is being pasted.

**Bottom:** Show a single lonely server becoming the center of a growing network — first frame: one server alone; second frame: two servers connected; third frame: four servers connected in a mesh. Each frame shows the network growing. Caption: "Start small, grow big."

**Visual metaphor:** The peer address is like a phone number — you need someone's number to call them, and once you're connected, you can meet everyone else through them.

---

## Page 10: Using Pollen

### Text

**Using Pollen**

Open your browser and go to:

```
http://your-server-ip:5000
```

You'll see a chat interface. Type a message and press Enter.

**What you can do:**

- **Chat** — have a conversation with the AI model. It streams responses word by word.
- **Adjust settings** — click the gear icon to change temperature (creativity), response length, and other parameters.
- **Check the network** — click the network icon to see how many peers are connected and which model blocks are covered.
- **Generate images** — if enabled, type an image description and the AI will create a picture.
- **Multiple conversations** — use the sidebar to start new chats and switch between them.

**From IRC or Matrix:**

If you enabled the bots, the AI is also available in your IRC channel or Matrix room. Just mention the bot's name:

```
PollenBot: what is the speed of light?
```

### Illustrator notes

**Main illustration:** A large, detailed mockup of the Pollen chat UI on a phone screen. Dark theme. Show:
- A sidebar peeking out from the left with conversation titles
- A chat with 2-3 message pairs (user message, AI response)
- The AI response mid-stream (partial text with a blinking cursor)
- The settings gear icon in the top right
- The network status icon showing a green dot (healthy)

**Below the phone mockup:** Two smaller panels side by side:
- Left: An IRC client window showing the bot responding in a channel
- Right: A Matrix/Element window showing the bot responding in a room

**Small callout bubbles** pointing to UI elements: "Gear = settings", "Graph = network health", "Sidebar = conversations"

---

## Page 11: What's next

### Text

**You're in the swarm**

Your computer is now part of a decentralized AI network. Every GPU in the cluster makes the model faster and more reliable for everyone.

**Things to try:**

- Add more computers to make the network stronger
- Enable IRC or Matrix bots to share the AI with your community
- Try different models by changing `MODEL_REPO` in `.env`
- Check the health dashboard at `/api/status` to monitor your network
- Read the full docs in `README.md` for all the configuration options

**Get help:**

- GitHub Issues: report bugs, request features
- Check the troubleshooting section in the README
- Join the Petals community at petals.dev

### Illustrator notes

A celebratory closing illustration. Show a healthy, thriving network — many glowing nodes connected in an organic mesh, with data particles flowing through the connections. Some nodes have GPU icons, some have chat bubbles coming off them (representing users chatting). The whole thing should feel warm, alive, collaborative.

At the bottom, simple icons linking to: GitHub (octocat), README (document icon), Petals community (globe icon).

Maybe a small "You are here" marker on one of the nodes in the network, with a friendly arrow pointing to it.

---

## Production Notes

### Format specifications

- **Target:** Mobile web (responsive, 375px minimum width)
- **Scroll:** Vertical, one page per screen height on mobile
- **Illustrations:** SVG preferred for crisp rendering at all sizes. PNG fallback at 2x resolution (750px wide minimum).
- **Animation:** Optional. If used, keep subtle — glowing effects, gentle particle movement, data flowing through connections. No autoplay video. Prefer CSS animation over JavaScript.
- **Accessibility:** All illustrations need alt text. Text should be readable without images. Maintain 4.5:1 contrast ratio minimum.
- **Dark mode:** Primary design. Light mode optional but plan for it in illustration color choices (avoid pure black backgrounds that can't invert cleanly).

### Illustration style guidance

- Clean, geometric, slightly rounded shapes
- Not cartoonish, not photorealistic — think technical illustration with personality
- Consistent line weight throughout
- Limited color palette: 4-5 colors max per illustration plus neutrals
- Servers/computers should look friendly and approachable, not intimidating
- The network should look organic and natural, not like a rigid grid
- Data flowing through the network should look like light or energy, not like documents or files
- Avoid stereotypical "hacker" or "cyberpunk" aesthetics — this should feel welcoming to non-technical people
