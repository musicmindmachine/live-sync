import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { api } from "./_generated/api";

const http = httpRouter();

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json",
      "access-control-allow-origin": "*",
    },
  });
}

http.route({
  path: "/sync/health",
  method: "GET",
  handler: httpAction(async () => jsonResponse({ ok: true })),
});

http.route({
  path: "/sync/push",
  method: "POST",
  handler: httpAction(async (ctx: any, request: Request) => {
    try {
      const body = await request.json();
      const result = await ctx.runMutation(api.sync.pushOps, body);
      return jsonResponse(result);
    } catch (error) {
      return jsonResponse(
        { error: error instanceof Error ? error.message : "Invalid push payload" },
        400,
      );
    }
  }),
});

http.route({
  path: "/sync/pull",
  method: "POST",
  handler: httpAction(async (ctx: any, request: Request) => {
    try {
      const body = await request.json();
      const result = await ctx.runQuery(api.sync.pullOps, body);
      return jsonResponse(result);
    } catch (error) {
      return jsonResponse(
        { error: error instanceof Error ? error.message : "Invalid pull payload" },
        400,
      );
    }
  }),
});

http.route({
  path: "/media/register-reference",
  method: "POST",
  handler: httpAction(async (ctx: any, request: Request) => {
    try {
      const body = await request.json();
      const result = await ctx.runMutation(api.media.upsertMediaReference, body);
      return jsonResponse(result);
    } catch (error) {
      return jsonResponse(
        { error: error instanceof Error ? error.message : "Invalid media reference payload" },
        400,
      );
    }
  }),
});

http.route({
  path: "/media/prepare-upload",
  method: "POST",
  handler: httpAction(async (ctx: any, request: Request) => {
    try {
      const body = await request.json();
      const result = await ctx.runMutation(api.media.prepareMediaUpload, body);
      return jsonResponse(result);
    } catch (error) {
      return jsonResponse(
        { error: error instanceof Error ? error.message : "Invalid media upload payload" },
        400,
      );
    }
  }),
});

http.route({
  path: "/media/complete-upload",
  method: "POST",
  handler: httpAction(async (ctx: any, request: Request) => {
    try {
      const body = await request.json();
      const result = await ctx.runAction(api.media.finalizeMediaUpload, body);
      return jsonResponse(result);
    } catch (error) {
      return jsonResponse(
        { error: error instanceof Error ? error.message : "Invalid media completion payload" },
        400,
      );
    }
  }),
});

http.route({
  path: "/media/pull",
  method: "POST",
  handler: httpAction(async (ctx: any, request: Request) => {
    try {
      const body = await request.json();
      const result = await ctx.runQuery(api.media.pullRoomMedia, body);
      return jsonResponse(result);
    } catch (error) {
      return jsonResponse(
        { error: error instanceof Error ? error.message : "Invalid media pull payload" },
        400,
      );
    }
  }),
});

http.route({
  path: "/media/download-url",
  method: "POST",
  handler: httpAction(async (ctx: any, request: Request) => {
    try {
      const body = await request.json();
      const result = await ctx.runAction(api.media.getMediaDownloadUrl, body);
      return jsonResponse(result);
    } catch (error) {
      return jsonResponse(
        { error: error instanceof Error ? error.message : "Invalid media download payload" },
        400,
      );
    }
  }),
});

http.route({
  path: "/media/download",
  method: "GET",
  handler: httpAction(async (ctx: any, request: Request) => {
    try {
      const url = new URL(request.url);
      const roomId = url.searchParams.get("roomId");
      const contentHash = url.searchParams.get("contentHash");
      if (!roomId || !contentHash) {
        return jsonResponse({ error: "roomId and contentHash are required" }, 400);
      }
      const result = await ctx.runAction(api.media.getMediaDownloadUrl, {
        roomId,
        contentHash,
      });
      return Response.redirect(result.url, 302);
    } catch (error) {
      return jsonResponse(
        { error: error instanceof Error ? error.message : "Unable to resolve media download" },
        400,
      );
    }
  }),
});

export default http;
