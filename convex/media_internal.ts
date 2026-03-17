import { internalMutation, internalQuery } from "./_generated/server";
import { v } from "convex/values";

async function ensureRoom(ctx: any, roomId: string) {
  let room = await ctx.db
    .query("rooms")
    .withIndex("by_room_id", (q: any) => q.eq("roomId", roomId))
    .unique();

  if (!room) {
    const now = Date.now();
    const roomDocId = await ctx.db.insert("rooms", {
      roomId,
      nextSequence: 1,
      stateJson: "{}",
      clockJson: "{}",
      compactedThroughSequence: 0,
      liveOpCount: 0,
      compactionScheduled: false,
      maxLamport: 0,
      mediaVersion: 0,
      mediaUpdatedAt: now,
      updatedAt: now,
    });
    room = await ctx.db.get(roomDocId);
  }

  if (!room) {
    throw new Error(`Failed to initialize room ${roomId}`);
  }

  return room;
}

async function bumpMediaVersion(ctx: any, room: any, now: number) {
  const mediaVersion = (room.mediaVersion ?? 0) + 1;
  await ctx.db.patch(room._id, {
    mediaVersion,
    mediaUpdatedAt: now,
  });
  return mediaVersion;
}

export const getAssetForFinalize = internalQuery({
  args: {
    roomId: v.string(),
    contentHash: v.string(),
  },
  handler: async (ctx, args) => {
    return await ctx.db
      .query("mediaAssets")
      .withIndex("by_room_hash", (q) => q.eq("roomId", args.roomId).eq("contentHash", args.contentHash))
      .unique();
  },
});

export const markAssetReady = internalMutation({
  args: {
    roomId: v.string(),
    clientId: v.string(),
    contentHash: v.string(),
    r2Key: v.string(),
    contentType: v.optional(v.string()),
    size: v.optional(v.number()),
    sha256: v.optional(v.string()),
  },
  handler: async (ctx, args) => {
    const room = await ensureRoom(ctx, args.roomId);
    const now = Date.now();
    const asset = await ctx.db
      .query("mediaAssets")
      .withIndex("by_room_hash", (q) => q.eq("roomId", args.roomId).eq("contentHash", args.contentHash))
      .unique();

    if (asset) {
      await ctx.db.patch(asset._id, {
        status: "ready",
        r2Key: args.r2Key,
        contentType: args.contentType ?? asset.contentType,
        size: args.size ?? asset.size,
        sha256: args.sha256 ?? asset.sha256,
        uploadedByClientId: args.clientId,
        updatedAt: now,
      });
    } else {
      await ctx.db.insert("mediaAssets", {
        roomId: args.roomId,
        contentHash: args.contentHash,
        status: "ready",
        r2Key: args.r2Key,
        contentType: args.contentType,
        size: args.size,
        sha256: args.sha256,
        uploadedByClientId: args.clientId,
        updatedAt: now,
      });
    }

    const mediaVersion = await bumpMediaVersion(ctx, room, now);
    return {
      roomId: args.roomId,
      contentHash: args.contentHash,
      mediaVersion,
    };
  },
});
