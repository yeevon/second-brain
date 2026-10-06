export type DecodedWikilink =
  | { ok: true; target: string }
  | { ok: false; target: string };

export const decodeWikilink = (href: string): DecodedWikilink => {
  const encoded = href.startsWith('wikilink:') ? href.slice('wikilink:'.length) : href;
  try {
    return { ok: true, target: decodeURIComponent(encoded) };
  } catch {
    return { ok: false, target: encoded };
  }
};
