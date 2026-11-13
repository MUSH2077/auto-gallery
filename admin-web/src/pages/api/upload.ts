import type { NextApiRequest, NextApiResponse } from "next";
import { forwardUpload } from "@/lib/server/uploadProxy";

// Next must leave the multipart body as the original Node stream.
export const config = { api: { bodyParser: false, responseLimit: false, externalResolver: true } };

export default function upload(request: NextApiRequest, response: NextApiResponse) {
  forwardUpload(request, response);
}
