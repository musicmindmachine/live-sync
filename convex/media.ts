import { R2 } from "@convex-dev/r2";
import { components, internal } from "./_generated/api";
import { action, mutation, query } from "./_generated/server";
import { v } from "convex/values";

const r2 = new R2(components.r2);

function compareReferenceVersions(
  left: { lamport: number; clientId: string; referenceId: string },
  right: { lamport: number; clientId: string; referenceId: string },
): number {
  if (left.lamport !== right.lamport) {
    return left.lamport < right.lamport ? -1 : 1;
  }
  if (left.clientId !== right.clientId) {
    return left.clientId < right.clientId ? -1 : 1;
  }
  if (left.referenceId !== right.referenceId) {
    return left.referenceId < right.referenceId ? -1 : 1;
  }
  return 0;
}

function sanitizePathSegment(value: string): string {
  return value.replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "") || "file";
}

function buildObjectKey(roomId: string, contentHash: string, relativePath: string): string {
  const segments = relativePath.split("/").filter(Boolean);
  const fileName = sanitizePathSegment(segments[segments.length - 1] || contentHash);
  return `rooms/${sanitizePathSegment(roomId)}/${contentHash}/${fileName}`;
}

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

  const needsHydration =
    room.stateJson === undefined ||
    room.clockJson === undefined ||
    room.compactedThroughSequence === undefined ||
    room.liveOpCount === undefined ||
    room.compactionScheduled === undefined ||
    room.maxLamport === undefined ||
    room.mediaVersion === undefined ||
    room.mediaUpdatedAt === undefined;

  if (needsHydration) {
    const hydrated = {
      stateJson: room.stateJson ?? "{}",
      clockJson: room.clockJson ?? "{}",
      compactedThroughSequence: room.compactedThroughSequence ?? 0,
      liveOpCount: room.liveOpCount ?? 0,
      compactionScheduled: room.compactionScheduled ?? false,
      maxLamport: room.maxLamport ?? 0,
      mediaVersion: room.mediaVersion ?? 0,
      mediaUpdatedAt: room.mediaUpdatedAt ?? room.updatedAt ?? Date.now(),
    };
    await ctx.db.patch(room._id, hydrated);
    return {
      ...room,
      ...hydrated,
    };
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

export const upsertMediaReference = mutation({
  args: {
    roomId: v.string(),
    clientId: v.string(),
    referenceId: v.string(),
    lomPath: v.string(),
    relativePath: v.string(),
    role: v.string(),
    contentHash: v.string(),
    contentType: v.optional(v.string()),
    size: v.optional(v.number()),
    lamport: v.number(),
  },
  handler: async (ctx, args) => {
    const room = await ensureRoom(ctx, args.roomId);
    const now = Date.now();
    const existing = await ctx.db
      .query("mediaReferences")
      .withIndex("by_room_reference", (q) => q.eq("roomId", args.roomId).eq("referenceId", args.referenceId))
      .unique();

    let updated = false;
    if (!existing) {
      await ctx.db.insert("mediaReferences", {
        roomId: args.roomId,
        referenceId: args.referenceId,
        lomPath: args.lomPath,
        relativePath: args.relativePath,
        role: args.role,
        contentHash: args.contentHash,
        contentType: args.contentType,
        size: args.size,
        updatedLamport: args.lamport,
        updatedByClientId: args.clientId,
        updatedAt: now,
      });
      updated = true;
    } else {
      const comparison = compareReferenceVersions(
        {
          lamport: existing.updatedLamport,
          clientId: existing.updatedByClientId,
          referenceId: existing.referenceId,
        },
        {
          lamport: args.lamport,
          clientId: args.clientId,
          referenceId: args.referenceId,
        },
      );
      if (comparison <= 0) {
        await ctx.db.patch(existing._id, {
          lomPath: args.lomPath,
          relativePath: args.relativePath,
          role: args.role,
          contentHash: args.contentHash,
          contentType: args.contentType,
          size: args.size,
          updatedLamport: args.lamport,
          updatedByClientId: args.clientId,
          updatedAt: now,
        });
        updated = true;
      }
    }

    let asset = await ctx.db
      .query("mediaAssets")
      .withIndex("by_room_hash", (q) => q.eq("roomId", args.roomId).eq("contentHash", args.contentHash))
      .unique();

    if (!asset) {
      await ctx.db.insert("mediaAssets", {
        roomId: args.roomId,
        contentHash: args.contentHash,
        status: "pending",
        r2Key: buildObjectKey(args.roomId, args.contentHash, args.relativePath),
        relativePathHint: args.relativePath,
        contentType: args.contentType,
        size: args.size,
        updatedAt: now,
      });
      asset = await ctx.db
        .query("mediaAssets")
        .withIndex("by_room_hash", (q) => q.eq("roomId", args.roomId).eq("contentHash", args.contentHash))
        .unique();
      updated = true;
    }

    const mediaVersion = updated ? await bumpMediaVersion(ctx, room, now) : room.mediaVersion ?? 0;
    return {
      updated,
      mediaVersion,
      assetStatus: asset?.status ?? "pending",
      r2Key: asset?.r2Key,
    };
  },
});

export const prepareMediaUpload = mutation({
  args: {
    roomId: v.string(),
    clientId: v.string(),
    contentHash: v.string(),
    relativePath: v.string(),
    contentType: v.optional(v.string()),
    size: v.optional(v.number()),
  },
  handler: async (ctx, args) => {
    const room = await ensureRoom(ctx, args.roomId);
    const now = Date.now();
    const existing = await ctx.db
      .query("mediaAssets")
      .withIndex("by_room_hash", (q) => q.eq("roomId", args.roomId).eq("contentHash", args.contentHash))
      .unique();

    if (existing && existing.status === "ready") {
      return {
        uploadRequired: false,
        status: "ready",
        r2Key: existing.r2Key,
      };
    }

    const r2Key = existing?.r2Key ?? buildObjectKey(args.roomId, args.contentHash, args.relativePath);
    const upload = await r2.generateUploadUrl(r2Key);

    if (existing) {
      await ctx.db.patch(existing._id, {
        r2Key,
        relativePathHint: existing.relativePathHint ?? args.relativePath,
        contentType: args.contentType ?? existing.contentType,
        size: args.size ?? existing.size,
        updatedAt: now,
      });
    } else {
      await ctx.db.insert("mediaAssets", {
        roomId: args.roomId,
        contentHash: args.contentHash,
        status: "pending",
        r2Key,
        relativePathHint: args.relativePath,
        contentType: args.contentType,
        size: args.size,
        uploadedByClientId: args.clientId,
        updatedAt: now,
      });
      await bumpMediaVersion(ctx, room, now);
    }

    return {
      uploadRequired: true,
      status: "pending",
      r2Key,
      uploadUrl: upload.url,
    };
  },
});

export const pullRoomMedia = query({
  args: {
    roomId: v.string(),
  },
  handler: async (ctx, args) => {
    const room = await ctx.db
      .query("rooms")
      .withIndex("by_room_id", (q) => q.eq("roomId", args.roomId))
      .unique();

    if (!room) {
      return {
        roomExists: false,
        mediaVersion: 0,
        references: [],
      };
    }

    const references = await ctx.db
      .query("mediaReferences")
      .withIndex("by_room_updated_at", (q) => q.eq("roomId", args.roomId))
      .collect();
    const assets = await ctx.db
      .query("mediaAssets")
      .withIndex("by_room_updated_at", (q) => q.eq("roomId", args.roomId))
      .collect();
    const assetsByHash = new Map(assets.map((asset) => [asset.contentHash, asset]));

    return {
      roomExists: true,
      mediaVersion: room.mediaVersion ?? 0,
      references: references.map((reference) => {
        const asset = assetsByHash.get(reference.contentHash);
        return {
          referenceId: reference.referenceId,
          lomPath: reference.lomPath,
          relativePath: reference.relativePath,
          role: reference.role,
          contentHash: reference.contentHash,
          contentType: reference.contentType ?? asset?.contentType,
          size: reference.size ?? asset?.size,
          updatedLamport: reference.updatedLamport,
          updatedByClientId: reference.updatedByClientId,
          assetStatus: asset?.status ?? "pending",
        };
      }),
    };
  },
});

export const finalizeMediaUpload = action({
  args: {
    roomId: v.string(),
    clientId: v.string(),
    contentHash: v.string(),
  },
  handler: async (ctx, args): Promise<{
    ready: boolean;
    r2Key: string;
    contentType?: string;
    size?: number;
  }> => {
    const asset: any = await ctx.runQuery(internal.media_internal.getAssetForFinalize, {
      roomId: args.roomId,
      contentHash: args.contentHash,
    });
    if (!asset) {
      throw new Error(`No media asset placeholder found for ${args.contentHash}`);
    }

    await r2.syncMetadata(ctx, asset.r2Key);
    const metadata = await r2.getMetadata(ctx, asset.r2Key);
    await ctx.runMutation(internal.media_internal.markAssetReady, {
      roomId: args.roomId,
      clientId: args.clientId,
      contentHash: args.contentHash,
      r2Key: asset.r2Key,
      contentType: metadata?.contentType ?? asset.contentType,
      size: metadata?.size ?? asset.size,
      sha256: metadata?.sha256 ?? asset.sha256,
    });

    return {
      ready: true,
      r2Key: asset.r2Key,
      contentType: metadata?.contentType ?? asset.contentType,
      size: metadata?.size ?? asset.size,
    };
  },
});

export const getMediaDownloadUrl = action({
  args: {
    roomId: v.string(),
    contentHash: v.string(),
    expiresIn: v.optional(v.number()),
  },
  handler: async (ctx, args): Promise<{ url: string; r2Key: string }> => {
    const asset: any = await ctx.runQuery(internal.media_internal.getAssetForFinalize, {
      roomId: args.roomId,
      contentHash: args.contentHash,
    });
    if (!asset || asset.status !== "ready") {
      throw new Error(`Media asset ${args.contentHash} is not ready`);
    }
    return {
      url: await r2.getUrl(asset.r2Key, { expiresIn: args.expiresIn }),
      r2Key: asset.r2Key,
    };
  },
});
