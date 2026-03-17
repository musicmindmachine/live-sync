import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  rooms: defineTable({
    roomId: v.string(),
    nextSequence: v.number(),
    stateJson: v.optional(v.string()),
    clockJson: v.optional(v.string()),
    compactedThroughSequence: v.optional(v.number()),
    liveOpCount: v.optional(v.number()),
    compactionScheduled: v.optional(v.boolean()),
    maxLamport: v.optional(v.number()),
    mediaVersion: v.optional(v.number()),
    mediaUpdatedAt: v.optional(v.number()),
    updatedAt: v.number(),
  }).index("by_room_id", ["roomId"]),

  operations: defineTable({
    roomId: v.string(),
    sequence: v.number(),
    opId: v.string(),
    clientId: v.string(),
    lamport: v.number(),
    kind: v.union(v.literal("set"), v.literal("delete")),
    path: v.string(),
    valueJson: v.optional(v.string()),
    createdAt: v.number(),
  })
    .index("by_room_sequence", ["roomId", "sequence"])
    .index("by_room_op_id", ["roomId", "opId"]),

  mediaAssets: defineTable({
    roomId: v.string(),
    contentHash: v.string(),
    status: v.union(v.literal("pending"), v.literal("ready")),
    r2Key: v.string(),
    relativePathHint: v.optional(v.string()),
    contentType: v.optional(v.string()),
    size: v.optional(v.number()),
    sha256: v.optional(v.string()),
    uploadedByClientId: v.optional(v.string()),
    updatedAt: v.number(),
  })
    .index("by_room_hash", ["roomId", "contentHash"])
    .index("by_room_updated_at", ["roomId", "updatedAt"]),

  mediaReferences: defineTable({
    roomId: v.string(),
    referenceId: v.string(),
    lomPath: v.string(),
    relativePath: v.string(),
    role: v.string(),
    contentHash: v.string(),
    contentType: v.optional(v.string()),
    size: v.optional(v.number()),
    updatedLamport: v.number(),
    updatedByClientId: v.string(),
    updatedAt: v.number(),
  })
    .index("by_room_reference", ["roomId", "referenceId"])
    .index("by_room_hash", ["roomId", "contentHash"])
    .index("by_room_updated_at", ["roomId", "updatedAt"]),
});
