export interface ClipboardWriter {
  writeText(text: string): Promise<void>;
}

export async function writeClipboardText(
  text: string,
  clipboard: ClipboardWriter | undefined = typeof navigator === "undefined" ? undefined : navigator.clipboard,
): Promise<string> {
  if (!clipboard || typeof clipboard.writeText !== "function") {
    throw new Error("Clipboard unavailable");
  }
  await clipboard.writeText(text);
  return text;
}
