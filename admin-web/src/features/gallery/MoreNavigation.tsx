"use client";
import type { RefObject } from "react";
import Link from "next/link";
import * as Popover from "@radix-ui/react-popover";
import { NotificationBell } from "@/components/NotificationCenter";
import { Icon } from "@/features/gallery-ui/Icon";
import { useGalleryCopy } from "@/features/gallery-ui/copy";
import { ADMIN_LINK_MODULE, ADMIN_NAV_LINKS, findAdminNavLink, hasAdminPermission } from "@/lib/adminNavigation";
import { adminRoutes } from "@/lib/adminRoutes";
import { useT } from "@/lib/i18n";

export default function MoreNavigation({ open, onOpenChange, triggerRef, pathname, has, isAdmin, onSearch }: {
  open: boolean; onOpenChange: (open: boolean) => void; triggerRef: RefObject<HTMLButtonElement | null>; pathname: string; has: (module: string) => boolean; isAdmin: boolean; onSearch: () => void;
}) {
  const { copy } = useGalleryCopy();
  const t = useT();
  const active = findAdminNavLink(pathname)?.href;
  const links = ADMIN_NAV_LINKS.filter(link => link.href !== adminRoutes.works && link.href !== adminRoutes.jobs && link.href !== adminRoutes.notifications
    && (!link.adminOnly || isAdmin) && hasAdminPermission(ADMIN_LINK_MODULE[link.href], has));
  const groups = [
    { label: t("nav.library"), links: links.filter(link => link.context === "library" || link.context === "source-management") },
    { label: t("nav.ingestion"), links: links.filter(link => link.context === "ingestion") },
    { label: t("nav.admin"), links: links.filter(link => link.context !== "library" && link.context !== "source-management" && link.context !== "ingestion") },
  ];
  return <Popover.Root open={open} onOpenChange={onOpenChange}>
    <Popover.Trigger asChild><button ref={triggerRef} type="button" className="gallery-more-trigger" aria-current={links.some(link => link.href === active) ? "page" : undefined}><Icon name="list"/><span>{copy("more")}</span></button></Popover.Trigger>
    <Popover.Content className="layout-tool-menu gallery-more-menu" aria-label={copy("more")} side="top" align="end" sideOffset={10} collisionPadding={16}>
      {(has("library") || has("tasks") || has("system")) && <section><h2>{copy("quickActions")}</h2><div className="gallery-more-quick">
        {has("library") && <button type="button" className="text-button" onClick={onSearch}>{copy("quickJump")}</button>}
        {(has("tasks") || has("system")) && <NotificationBell/>}
      </div></section>}
      {groups.map(group => group.links.length > 0 && <section key={group.label}><h2>{group.label}</h2><div className="gallery-more-links">
        {group.links.map(link => <Link key={link.href} href={link.href} aria-current={active === link.href ? "page" : undefined} onClick={() => onOpenChange(false)}>{t(link.labelKey)}</Link>)}
      </div></section>)}
    </Popover.Content>
  </Popover.Root>;
}
