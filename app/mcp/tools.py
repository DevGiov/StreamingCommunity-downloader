"""Model Context Protocol (MCP) tools for StreamingCommunity Downloader.

Provides tools for AI agents to search content, get metadata/episodes,
download movies/series/anime, manage jobs, track followed series, and check system status.
"""

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import Literal

from mcp.server.mcpserver import MCPServer

from app import __version__
from app import downloads_notify
from app.config import VIDEOS_DIR, configured_domain, read_data
from app.core import animeunity, home, metadata, page, tv
from app.jobs import job_manager
from app.requests import models as request_models, service as requests_service
from app.watches import models as watch_models, poller as watch_poller

logger = logging.getLogger(__name__)

mcp_server = MCPServer(
    name="StreamingCommunity Downloader",
    version=__version__,
    instructions=(
        "You are an AI assistant interacting with StreamingCommunity Downloader.\n"
        "Use these tools to discover, download and monitor movies, TV series, and anime,\n"
        "as well as managing download queues and followed series."
    ),
)


# ── Discovery & Search ─────────────────────────────────────────────────────────

@mcp_server.tool()
async def search_content(
    query: str,
    source: Literal["all", "streamingcommunity", "animeunity"] = "all",
    kind: Literal["all", "movie", "tv", "anime"] = "all",
    page_num: int = 1,
) -> dict:
    """Search for movies, TV series, and anime across StreamingCommunity and AnimeUnity.

    Args:
        query: The search text (e.g. 'Inception', 'Breaking Bad', 'One Piece').
        source: 'all', 'streamingcommunity', or 'animeunity'. Default is 'all'.
        kind: Filter by type: 'all', 'movie', 'tv', or 'anime'.
        page_num: Result page number (default 1).
    """
    domain = configured_domain()
    results = []
    errors = []

    # 1. StreamingCommunity search
    if source in ("all", "streamingcommunity"):
        if not domain:
            errors.append("StreamingCommunity: No domain configured")
        else:
            try:
                media_filter = "movie" if kind == "movie" else "tv" if kind == "tv" else None
                sc_items = await asyncio.to_thread(
                    page.search, query, domain, page=page_num, media_type=media_filter
                )
                for item in sc_items:
                    results.append({
                        "id": item.get("id"),
                        "slug": item.get("slug"),
                        "title": item.get("name"),
                        "type": item.get("type"),
                        "year": (item.get("release_date") or item.get("last_air_date") or "")[:4],
                        "source": "streamingcommunity",
                        "poster": item.get("poster"),
                        "score": item.get("score"),
                        "seasons_count": item.get("seasons_count", 0),
                    })
            except Exception as e:
                logger.warning("StreamingCommunity search error: %s", e)
                errors.append(f"StreamingCommunity: {str(e)}")

    # 2. AnimeUnity search
    if source in ("all", "animeunity") and kind in ("all", "anime"):
        try:
            au_items = await asyncio.to_thread(animeunity.search, query, page=page_num)
            for item in au_items:
                results.append({
                    "id": item.get("id"),
                    "slug": item.get("slug"),
                    "title": item.get("name"),
                    "type": "anime",
                    "year": (item.get("release_date") or "")[:4],
                    "source": "animeunity",
                    "poster": item.get("poster"),
                    "score": item.get("score"),
                    "episodes_count": item.get("episodes_count", 0),
                })
        except Exception as e:
            logger.warning("AnimeUnity search error: %s", e)
            errors.append(f"AnimeUnity: {str(e)}")

    return {
        "query": query,
        "page": page_num,
        "count": len(results),
        "results": results,
        "errors": errors if errors else None,
    }


@mcp_server.tool()
async def get_content_details(
    content_id: str,
    slug: str = "",
    media_type: Literal["movie", "tv", "anime"] = "movie",
    source: Literal["streamingcommunity", "animeunity"] = "streamingcommunity",
) -> dict:
    """Get full details and metadata for a title (plot, genres, rating, artwork, trailer, etc.).

    Args:
        content_id: The ID of the movie/series/anime.
        slug: The title slug (e.g. 'breaking-bad').
        media_type: 'movie', 'tv', or 'anime'.
        source: 'streamingcommunity' or 'animeunity'.
    """
    domain = configured_domain()
    try:
        if source == "streamingcommunity":
            if not domain:
                return {"error": "No domain configured", "id": content_id, "slug": slug}
            version = (await asyncio.to_thread(page.get_domain_version, domain)) or ""
            meta_type = "movie" if media_type == "movie" else "tv"
            meta = await asyncio.to_thread(metadata.title_metadata, meta_type, content_id, slug, version)
            return {
                "id": content_id,
                "slug": slug,
                "source": source,
                "media_type": meta_type,
                "title": meta.get("name") or meta.get("title"),
                "plot": meta.get("plot"),
                "genres": meta.get("genres", []),
                "score": meta.get("score"),
                "year": meta.get("year"),
                "trailer": meta.get("trailer"),
                "poster": meta.get("poster"),
                "backdrop": meta.get("backdrop"),
                "tmdb_id": meta.get("tmdb_id"),
            }
        else:
            items = await asyncio.to_thread(animeunity.search, slug or content_id)
            match = next((i for i in items if str(i.get("id")) == str(content_id) or i.get("slug") == slug), None)
            if not match and items:
                match = items[0]
            if match:
                return {
                    "id": match.get("id"),
                    "slug": match.get("slug"),
                    "source": "animeunity",
                    "media_type": "anime",
                    "title": match.get("name"),
                    "plot": match.get("plot"),
                    "genres": match.get("genres", []),
                    "score": match.get("score"),
                    "year": (match.get("release_date") or "")[:4],
                    "poster": match.get("poster"),
                    "backdrop": match.get("backdrop"),
                    "episodes_count": match.get("episodes_count", 0),
                    "studio": match.get("studio"),
                }
            return {"id": content_id, "slug": slug, "source": "animeunity", "error": "Anime not found"}
    except Exception as e:
        logger.error("Error fetching content details: %s", e)
        return {"error": str(e), "id": content_id, "slug": slug}


@mcp_server.tool()
async def get_series_episodes(
    tv_id: int,
    slug: str,
    season_number: int | None = None,
) -> dict:
    """Retrieve seasons and episodes for a TV series on StreamingCommunity.

    Args:
        tv_id: The StreamingCommunity series ID.
        slug: Series slug.
        season_number: Optional season number (e.g. 1). If omitted, returns all seasons with their episodes.
    """
    domain = configured_domain()
    if not domain:
        return {"error": "No domain configured"}

    try:
        def _fetch():
            version = page.get_domain_version(domain) or ""
            token = tv.get_token(tv_id, domain)
            seasons_count = tv.get_info_tv(tv_id, slug, version, domain)
            output_seasons = []
            target_seasons = [season_number] if season_number is not None else list(range(1, seasons_count + 1))
            for s_num in target_seasons:
                if s_num < 1 or s_num > seasons_count:
                    continue
                raw_episodes = tv.get_info_season(tv_id, slug, domain, version, token, s_num) or []
                output_seasons.append({
                    "season_number": s_num,
                    "episodes_count": len(raw_episodes),
                    "episodes": raw_episodes,
                })
            return output_seasons

        seasons_data = await asyncio.to_thread(_fetch)
        return {"tv_id": tv_id, "slug": slug, "seasons": seasons_data}
    except Exception as e:
        logger.error("Error getting series episodes: %s", e)
        return {"error": str(e), "tv_id": tv_id, "slug": slug}


@mcp_server.tool()
async def get_anime_episodes(anime_id: int | str, slug: str = "") -> dict:
    """Retrieve all available episodes for an anime from AnimeUnity.

    Args:
        anime_id: The AnimeUnity anime ID.
        slug: Optional anime slug.
    """
    try:
        episodes = await asyncio.to_thread(animeunity.get_episodes, str(anime_id))
        return {
            "anime_id": anime_id,
            "slug": slug,
            "episodes_count": len(episodes),
            "episodes": episodes,
        }
    except Exception as e:
        logger.error("Error getting anime episodes: %s", e)
        return {"error": str(e), "anime_id": anime_id}


@mcp_server.tool()
async def get_home_shelves(
    source: Literal["streamingcommunity", "animeunity"] = "streamingcommunity"
) -> dict:
    """Get the front page shelves (trending, new releases, popular) from the source.

    Args:
        source: 'streamingcommunity' or 'animeunity'.
    """
    domain = configured_domain()
    if source == "streamingcommunity" and not domain:
        return {"error": "No source domain configured on panel"}
    try:
        shelves = await asyncio.to_thread(home.shelves, source, domain)
        return {"source": source, "shelves": shelves}
    except Exception as e:
        logger.error("Error fetching home shelves: %s", e)
        return {"error": str(e), "source": source}


# ── Download Operations ────────────────────────────────────────────────────────

@mcp_server.tool()
async def download_film(
    film_id: int,
    title: str,
    year: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = ["ita", "eng"],
) -> dict:
    """Start or queue downloading a movie from StreamingCommunity.

    Args:
        film_id: The movie ID.
        title: Title of the film.
        year: Release year (optional).
        audio_languages: Preferred audio languages list (e.g. ['ita']).
        subtitle_languages: Preferred subtitle languages list (e.g. ['ita']).
    """
    domain = configured_domain()
    if not domain:
        return {"error": "No source domain configured on panel"}

    try:
        def _submit():
            tmdb_id = None
            try:
                tmdb_id = metadata.cached_tmdb_id("movie", film_id)
            except Exception:
                pass
            return job_manager.submit_film(
                film_id,
                title,
                domain,
                year=year,
                audio_languages=audio_languages,
                subtitle_languages=subtitle_languages,
                user_id=None,
                tmdb_id=tmdb_id,
            )

        job_id = await asyncio.to_thread(_submit)
        return {
            "status": "queued",
            "job_id": job_id,
            "title": title,
            "type": "film",
        }
    except Exception as e:
        logger.error("Failed to submit film download: %s", e)
        return {"error": str(e)}


@mcp_server.tool()
async def download_episode(
    tv_id: int,
    slug: str,
    tv_name: str,
    season_number: int,
    episode_number: int,
    year: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = ["ita", "eng"],
) -> dict:
    """Start or queue downloading a specific TV episode from StreamingCommunity.

    Args:
        tv_id: Series ID.
        slug: Series slug.
        tv_name: Name of the TV series.
        season_number: Season number (1-based).
        episode_number: Episode number (1-based).
        year: Year (optional).
        audio_languages: Preferred audio languages list.
        subtitle_languages: Preferred subtitle languages list.
    """
    domain = configured_domain()
    if not domain:
        return {"error": "No source domain configured on panel"}

    try:
        def _submit():
            version = page.get_domain_version(domain) or ""
            token = tv.get_token(tv_id, domain)
            episodes = tv.get_info_season(tv_id, slug, domain, version, token, season_number) or []

            # Find matching episode index
            target_idx = None
            for idx, ep in enumerate(episodes):
                ep_num = ep.get("n") if ep.get("n") is not None else ep.get("number")
                if str(ep_num) == str(episode_number):
                    target_idx = idx
                    break

            if target_idx is None:
                raise ValueError(
                    f"Episode {episode_number} not found in Season {season_number} of {tv_name}"
                )

            tmdb_id = None
            try:
                tmdb_id = metadata.cached_tmdb_id("tv", tv_id)
            except Exception:
                pass

            job_id = job_manager.submit_episode(
                tv_id,
                episodes,
                target_idx,
                domain,
                token,
                tv_name,
                season_number,
                year=year,
                audio_languages=audio_languages,
                subtitle_languages=subtitle_languages,
                user_id=None,
                tmdb_id=tmdb_id,
            )
            return job_id

        job_id = await asyncio.to_thread(_submit)
        label = f"{tv_name} S{season_number:02d}E{episode_number:02d}"
        return {
            "status": "queued",
            "job_id": job_id,
            "title": label,
            "type": "episode",
        }
    except Exception as e:
        logger.error("Failed to submit episode download: %s", e)
        return {"error": str(e)}


@mcp_server.tool()
async def download_season(
    tv_id: int,
    slug: str,
    tv_name: str,
    season_number: int,
    year: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = ["ita", "eng"],
) -> dict:
    """Download an entire TV season from StreamingCommunity in batch.

    Args:
        tv_id: Series ID.
        slug: Series slug.
        tv_name: Series title.
        season_number: Season number.
        year: Year (optional).
        audio_languages: Preferred audio tracks.
        subtitle_languages: Preferred subtitle tracks.
    """
    domain = configured_domain()
    if not domain:
        return {"error": "No source domain configured on panel"}

    try:
        def _submit_batch():
            version = page.get_domain_version(domain) or ""
            token = tv.get_token(tv_id, domain)
            episodes = tv.get_info_season(tv_id, slug, domain, version, token, season_number) or []
            if not episodes:
                raise ValueError(f"No episodes found for season {season_number}")

            batch_id = uuid.uuid4().hex
            label = f"{tv_name} — Stagione {season_number}"
            downloads_notify.register(
                batch_id, kind="season", label=label, expected=len(episodes), user_id=None
            )

            tmdb_id = None
            try:
                tmdb_id = metadata.cached_tmdb_id("tv", tv_id)
            except Exception:
                pass

            job_ids = []
            for idx in range(len(episodes)):
                jid = job_manager.submit_episode(
                    tv_id,
                    episodes,
                    idx,
                    domain,
                    token,
                    tv_name,
                    season_number,
                    year=year,
                    audio_languages=audio_languages,
                    subtitle_languages=subtitle_languages,
                    user_id=None,
                    batch_id=batch_id,
                    batch_kind="season",
                    batch_label=label,
                    tmdb_id=tmdb_id,
                )
                job_ids.append(jid)

            return batch_id, job_ids

        batch_id, job_ids = await asyncio.to_thread(_submit_batch)
        return {
            "status": "queued",
            "batch_id": batch_id,
            "count": len(job_ids),
            "job_ids": job_ids,
            "label": f"{tv_name} S{season_number:02d}",
        }
    except Exception as e:
        logger.error("Failed to submit season batch: %s", e)
        return {"error": str(e)}


@mcp_server.tool()
async def download_anime_episode(
    anime_id: str,
    anime_name: str,
    episode_id: int,
    episode_number: str | int,
    anime_type: str = "tv",
    year: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = ["ita", "eng"],
) -> dict:
    """Download a single anime episode from AnimeUnity.

    Args:
        anime_id: Anime ID on AnimeUnity.
        anime_name: Name of the anime.
        episode_id: Episode ID on AnimeUnity.
        episode_number: Number of the episode (e.g. '1', '12').
        anime_type: 'tv', 'movie', 'ova', etc.
        year: Year (optional).
        audio_languages: Audio languages.
        subtitle_languages: Subtitle languages.
    """
    try:
        def _submit():
            episode_dict = {"id": episode_id, "number": episode_number}
            return job_manager.submit_anime_episode(
                anime_id,
                episode_dict,
                anime_name,
                anime_type,
                year=year,
                audio_languages=audio_languages,
                subtitle_languages=subtitle_languages,
                user_id=None,
            )

        job_id = await asyncio.to_thread(_submit)
        return {
            "status": "queued",
            "job_id": job_id,
            "title": f"{anime_name} E{episode_number}",
            "type": "anime",
        }
    except Exception as e:
        logger.error("Failed to submit anime download: %s", e)
        return {"error": str(e)}


# ── Job Monitoring & Control ──────────────────────────────────────────────────

@mcp_server.tool()
def list_downloads(
    status: Literal["all", "running", "queued", "scheduled", "done", "error", "cancelled"] = "all",
    limit: int = 50,
) -> dict:
    """List download jobs and their current state.

    Args:
        status: Filter by status: 'all', 'running', 'queued', 'scheduled', 'done', 'error', 'cancelled'.
        limit: Max number of jobs to return (default 50).
    """
    jobs = job_manager.list_jobs()
    if status != "all":
        jobs = [j for j in jobs if j.get("status") == status]

    # Sort newest first
    jobs = sorted(jobs, key=lambda j: j.get("created_at", ""), reverse=True)[:limit]

    clean_jobs = []
    for j in jobs:
        clean_jobs.append({
            "job_id": j.get("job_id"),
            "title": j.get("title"),
            "type": j.get("type"),
            "status": j.get("status"),
            "pct": j.get("progress", {}).get("pct", 0),
            "speed": j.get("progress", {}).get("speed", 0),
            "eta": j.get("progress", {}).get("eta"),
            "output_path": j.get("output_path"),
            "error": j.get("error"),
            "created_at": j.get("created_at"),
        })

    return {"count": len(clean_jobs), "jobs": clean_jobs}


@mcp_server.tool()
def get_download_progress(job_id: str) -> dict:
    """Get live progress and details for a specific download job.

    Args:
        job_id: The job ID to inspect.
    """
    job = job_manager.get(job_id)
    if not job:
        return {"error": "Job not found", "job_id": job_id}

    return {
        "job_id": job.job_id,
        "title": job.title,
        "type": job.type,
        "status": job.status,
        "phases": job.phases,
        "progress": job.progress,
        "output_path": job.output_path,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


@mcp_server.tool()
def cancel_download(job_id: str) -> dict:
    """Cancel a scheduled, queued, or running download job.

    Args:
        job_id: The job ID to cancel.
    """
    success = job_manager.cancel(job_id)
    if success:
        return {"ok": True, "job_id": job_id, "message": "Download cancelled"}
    return {"ok": False, "job_id": job_id, "message": "Job cannot be cancelled or was not found"}


@mcp_server.tool()
def retry_download(job_id: str) -> dict:
    """Retry a failed download job. Creates a new job and removes the failed one.

    Args:
        job_id: The failed job ID to retry.
    """
    new_job_id = job_manager.retry(job_id)
    if new_job_id:
        return {"ok": True, "old_job_id": job_id, "new_job_id": new_job_id, "status": "queued"}
    return {"ok": False, "job_id": job_id, "message": "Job could not be retried (must be in 'error' state)"}


# ── Series Watch Management ───────────────────────────────────────────────────

@mcp_server.tool()
async def follow_series(
    source: Literal["streamingcommunity", "animeunity"],
    media_type: Literal["tv", "anime"],
    external_id: str,
    title: str,
    slug: str | None = None,
    year: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = [],
    auto_approve: bool = False,
) -> dict:
    """Follow a TV series or anime to automatically check and download newly published episodes.

    Args:
        source: 'streamingcommunity' or 'animeunity'.
        media_type: 'tv' or 'anime'.
        external_id: ID of the series on the source.
        title: Title of the series.
        slug: Series slug.
        year: Year (optional).
        audio_languages: Preferred audio tracks.
        subtitle_languages: Preferred subtitle tracks.
        auto_approve: If true, automatically approves downloads without manual human approval.
    """
    try:
        def _create_watch():
            watch, created = watch_models.create(
                source=source,
                media_type=media_type,
                external_id=str(external_id),
                title=title,
                slug=slug,
                year=year,
                audio_languages=audio_languages,
                subtitle_languages=subtitle_languages,
                created_by=None,
            )
            if auto_approve:
                watch_models.set_auto_approve(watch.id, True)

            if created:
                # Seed existing episodes so only new releases trigger downloads
                episodes = watch_poller.current_episodes(watch)
                if episodes:
                    watch_models.seed_seen(watch.id, [key for key, _ in episodes])
            return watch, created

        watch, created = await asyncio.to_thread(_create_watch)
        return {
            "ok": True,
            "watch_id": watch.id,
            "title": watch.title,
            "created": created,
            "auto_approve": watch.auto_approve,
        }
    except Exception as e:
        logger.error("Failed to follow series: %s", e)
        return {"ok": False, "error": str(e)}


@mcp_server.tool()
def list_watched_series() -> dict:
    """List all followed series and anime currently being monitored for new episodes."""
    watches = watch_models.list_all()
    results = []
    for w in watches:
        results.append({
            "watch_id": w.id,
            "title": w.title,
            "source": w.source,
            "media_type": w.media_type,
            "enabled": w.enabled,
            "auto_approve": w.auto_approve,
            "audio_languages": w.audio_languages,
            "last_checked_at": w.last_checked_at,
        })
    return {"count": len(results), "watches": results}


@mcp_server.tool()
def unfollow_series(watch_id: int) -> dict:
    """Stop following a series/anime.

    Args:
        watch_id: The ID of the watch to disable.
    """
    watch_models.disable(watch_id)
    return {"ok": True, "watch_id": watch_id, "message": "Watch disabled"}


@mcp_server.tool()
async def check_series_updates(watch_id: int | None = None) -> dict:
    """Trigger an immediate check for new episodes of followed series.

    Args:
        watch_id: Optional ID of a specific series to check. If omitted, checks all followed series.
    """
    try:
        if watch_id is not None:
            watch = watch_models.get(watch_id)
            if not watch:
                return {"ok": False, "error": f"Watch {watch_id} not found"}
            result = await asyncio.to_thread(watch_poller.poll_watch, watch)
            return {"ok": True, "message": f"Checked watch {watch.title}", "result": result}
        else:
            result = await asyncio.to_thread(watch_poller.run_poll_cycle)
            return {"ok": True, "message": "Checked all active watches", "result": result}
    except Exception as e:
        logger.error("Failed to check series updates: %s", e)
        return {"ok": False, "error": str(e)}


# ── Request Queue Operations ──────────────────────────────────────────────────

@mcp_server.tool()
async def submit_request(
    source: Literal["streamingcommunity", "animeunity"],
    media_type: Literal["film", "episode", "anime"],
    external_id: str,
    title: str,
    slug: str | None = None,
    year: str | None = None,
    poster: str | None = None,
    season: int | None = None,
    episode_number: str | None = None,
    audio_languages: list[str] = ["ita"],
    subtitle_languages: list[str] = [],
) -> dict:
    """Submit a request to the content request queue.

    Args:
        source: 'streamingcommunity' or 'animeunity'.
        media_type: 'film', 'episode', or 'anime'.
        external_id: External ID of the content.
        title: Title of the film, episode, or anime.
        slug: Content slug.
        year: Year.
        poster: Poster URL.
        season: Season number (if episode).
        episode_number: Episode number (if episode or anime).
        audio_languages: Preferred audio languages.
        subtitle_languages: Preferred subtitle languages.
    """
    try:
        def _create():
            from app.auth import models as auth_models
            users = auth_models.list_users()
            admin = next((u for u in users if u.is_jellyfin_admin), None)
            user = admin or (users[0] if users else None)
            if not user:
                raise ValueError("No user account found. The request queue requires Jellyfin user accounts to be configured.")

            req, created = requests_service.create_request(
                requested_by=user.id,
                source=source,
                media_type=media_type,
                external_id=str(external_id),
                title=title,
                slug=slug,
                year=year,
                poster=poster,
                season=season,
                episode_number=str(episode_number) if episode_number else None,
                audio_languages=audio_languages,
                subtitle_languages=subtitle_languages,
            )
            return req, created

        req, created = await asyncio.to_thread(_create)
        return {
            "ok": True,
            "request_id": req.id,
            "title": req.title,
            "status": req.status,
            "created": created,
        }
    except Exception as e:
        logger.error("Failed to submit request: %s", e)
        return {"ok": False, "error": str(e)}


@mcp_server.tool()
def list_requests(
    status: Literal["all", "pending", "approved", "downloading", "completed", "rejected", "needs_attention"] = "all",
    limit: int = 50,
) -> dict:
    """List content requests from the request queue.

    Args:
        status: Filter by status: 'all', 'pending', 'approved', 'downloading', 'completed', 'rejected', 'needs_attention'.
        limit: Max requests to return.
    """
    all_requests = request_models.list_all()
    if status != "all":
        all_requests = [r for r in all_requests if r.status == status]
    all_requests = all_requests[:limit]

    return {
        "count": len(all_requests),
        "requests": [r.to_public() for r in all_requests],
    }


@mcp_server.tool()
async def approve_request(request_id: int) -> dict:
    """Approve a pending request in the queue to initiate download.

    Args:
        request_id: The ID of the request to approve.
    """
    try:
        def _approve():
            from app.auth import models as auth_models
            users = auth_models.list_users()
            admin = next((u for u in users if u.is_jellyfin_admin), None)
            user = admin or (users[0] if users else None)
            if not user:
                raise ValueError("No user account found to approve requests.")

            req = requests_service.approve(request_id, decided_by=user.id)
            return req

        req = await asyncio.to_thread(_approve)
        return {"ok": True, "request_id": req.id, "status": req.status}
    except Exception as e:
        logger.error("Failed to approve request: %s", e)
        return {"ok": False, "error": str(e)}


# ── System Status & Libraries ─────────────────────────────────────────────────

@mcp_server.tool()
def get_system_status() -> dict:
    """Get panel health status: version, source domain validity, disk storage usage, and active job count."""
    domain = configured_domain()
    valid = False
    version = None
    if domain:
        try:
            version = page.get_domain_version(domain)
            valid = True
        except Exception:
            valid = False

    # Disk usage
    storage_path = VIDEOS_DIR if VIDEOS_DIR.exists() else (VIDEOS_DIR.parent if VIDEOS_DIR.parent.exists() else Path("."))
    try:
        total, used, free = shutil.disk_usage(storage_path)
        free_gb = round(free / (1024**3), 2)
        total_gb = round(total / (1024**3), 2)
        used_pct = round((used / total) * 100, 1) if total else 0.0
    except Exception:
        free_gb, total_gb, used_pct = 0.0, 0.0, 0.0

    # Active downloads
    active_jobs = [j for j in job_manager.list_jobs() if j.get("status") in ("running", "queued")]

    return {
        "panel_version": __version__,
        "source_domain": domain,
        "source_domain_valid": valid,
        "source_version": version,
        "storage": {
            "path": str(VIDEOS_DIR),
            "free_gb": free_gb,
            "total_gb": total_gb,
            "used_pct": round((used / total) * 100, 1),
        },
        "active_jobs_count": len(active_jobs),
    }


@mcp_server.tool()
def list_libraries() -> dict:
    """List configured Jellyfin destination libraries (film, tv, anime) and excluded folders."""
    data = read_data()
    return {
        "libraries": data.get("libraries", []),
        "excluded_folders": data.get("excluded_folders", []),
    }
