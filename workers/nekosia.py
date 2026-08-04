from shared import BaseDownloader
from pathlib import Path
import json, os
import asyncio


class NekosiaWorker(BaseDownloader):
    def __init__(self, tag, amount, net_config):
        super().__init__("nekosia", "Nekosia", amount, net_config)
        # Normalize tag: convert azki_(2nd_costume) -> azki, usada-pekora -> usada pekora
        import re
        norm = re.sub(r"[_-]+", " ", tag)
        norm = re.sub(r"\s*\(\d+(?:st|nd|rd|th)\s*costume\)", "", norm).strip()
        self.included_tags = [t.strip().lower() for t in norm.split() if t.strip()]
        self.exclusions = [t[1:] for t in norm.split() if t.startswith("-") and len(t) > 1]
        if not self.included_tags:
            self.included_tags = ["waifu"]
        self.full_tag = "_".join(self.included_tags)
        self.tag_dir = os.path.join(self.site_root, self.full_tag)
        self.rating = (net_config or {}).get("rating", "safe")
        self.rating_dir = os.path.join(self.tag_dir, self.rating.capitalize())
        os.makedirs(self.rating_dir, exist_ok=True)
        self.database_path = Path("database") / "nekosia_tag_names.json"

    def get_image(self, included_tags, blacklisted_tags=None, count=1, rating="safe"):
        if blacklisted_tags is None:
            blacklisted_tags = []
        if rating.lower() not in ("suggestive", "safe"):
            return "Invalid Argument: rating"
        if not included_tags:
            return "Included tags can't be empty"
        params = {"count": count, "additionalTags": ",".join(included_tags),
                  "blacklistedTags": ",".join(blacklisted_tags), "rating": rating}
        response = self.session.get("https://api.nekosia.cat/api/v1/images/search", params=params)
        if response.status_code not in (200, 400):
            return f"non 200 status code: {response.status_code}"
        try:
            data = response.json()
        except Exception:
            return f"invalid JSON response: {response.text[:200]}"
        if not data.get("success"):
            # 400 with success=false = no images, not a hard error
            if response.status_code == 400:
                return []
            return f"API error: {data.get('status')} {data.get('message', '')}"
        result = []
        # handle both single image or list of images (both possible in API response)
        images = data.get("images")
        if isinstance(images, dict):
            images = [images]
        for image in images or []:
            img = image.get("image", {})
            result.append({
                "id": image.get("id"),
                "url_orginal": img.get("original", {}).get("url"),
                "url_compressed": img.get("compressed", {}).get("url"),
                "category": image.get("category"),
                "tags": image.get("tags", []),
            })
        return result

    def update_nekosia_database(self):
        response = self.session.get("https://api.nekosia.cat/api/v1/tags").json()
        final_values = []
        if response.get("success"):
            for category in ("tags", "anime", "characters"):
                if not response.get(category):
                    return False
                final_values.extend(response[category])
            with open(str(self.database_path), "w") as f:
                json.dump(final_values, f)
            return True
        return False

    def load_database(self):
        if not self.database_path.exists():
            return []
        with open(str(self.database_path)) as f:
            return json.load(f)

    async def scraper_task(self):
        self.log(f"Initializing worker for tags: {self.included_tags}")
        self.log(f"Exclusions: {self.exclusions}")
        self.log(f"Rating: {self.rating}")
        need = self.amount or 200
        collected = 0
        while collected < need and not self.stop_event.is_set():
            raw = await asyncio.to_thread(self.get_image, self.included_tags, self.exclusions, 20, self.rating)
            if isinstance(raw, str):
                self.log(f"API error: {raw}")
                break
            if not raw:
                self.log("No images found.")
                break
            for item in raw:
                if self.stop_event.is_set() or collected >= need:
                    break
                url = item.get("url_orginal") or item.get("url_compressed")
                if not url:
                    continue
                ext = url.rsplit(".", 1)[-1].split("?")[0]
                filename = f"{item['id']}.{ext}"
                filepath = os.path.join(self.rating_dir, filename)
                tags = item.get("tags", []) + [self.full_tag]
                if await self.enqueue_download(url, filepath, filename, tags, []):
                    collected += 1
        if collected:
            self.log(f"Enqueued {collected} item{'s' if collected != 1 else ''}.")

    def run(self):
        asyncio.run(self.run_async_loop(self.scraper_task))
        self.log("--- Worker Terminated ---")


def worker_nekosia(tag, amount, net_config):
    NekosiaWorker(tag, amount, net_config).run()