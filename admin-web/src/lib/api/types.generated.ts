/**
 * Generated OpenAPI type entrypoint.
 *
 * Refresh with: npm run generate:api-types
 */
export interface paths {}

export interface components {
  schemas: {
    AssetRead: {
      id: string;
      file_name: string;
      file_size?: number | null;
      mime_type?: string | null;
      width?: number | null;
      height?: number | null;
      sha256?: string | null;
      thumb_sm_path?: string | null;
      thumb_md_path?: string | null;
      thumb_lg_path?: string | null;
      created_at: string;
    };
    CreatorRead: Record<string, unknown>;
    SubscriptionRead: Record<string, unknown>;
    WorkList: {
      id: string;
      title?: string | null;
      posted_at?: string | null;
      thumbnail_asset_id?: string | null;
      asset_count?: number;
      is_nsfw: boolean;
      is_ai_generated?: boolean;
      created_at: string;
      source?: string | null;
      creator_name?: string | null;
      creator_id?: string | null;
      has_ugoira?: boolean;
      preview_asset_ids?: string[];
      is_favorite?: boolean;
    };
    WorkRead: {
      id: string;
      title?: string | null;
      description?: string | null;
      posted_at?: string | null;
      thumbnail_asset_id?: string | null;
      asset_count?: number;
      is_nsfw: boolean;
      is_ai_generated?: boolean;
      is_favorite: boolean;
      created_at: string;
      updated_at: string;
    };
  };
}
