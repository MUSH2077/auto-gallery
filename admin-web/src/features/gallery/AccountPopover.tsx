"use client";

import type { ReactNode, RefObject } from "react";
import * as Popover from "@radix-ui/react-popover";

export default function AccountPopover({ open, onOpenChange, triggerRef, label, children }: {
  open: boolean; onOpenChange: (open: boolean) => void; triggerRef: RefObject<HTMLButtonElement | null>; label: string; children: ReactNode;
}) {
  return <Popover.Root open={open} onOpenChange={onOpenChange}><div className="gallery-account">
    <Popover.Trigger asChild><button ref={triggerRef} type="button" className="layout-tool-trigger gallery-account-trigger">{label}<svg className="layout-tool-chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></button></Popover.Trigger>
    <Popover.Content className="layout-tool-menu gallery-account-menu" aria-label={label} align="end" sideOffset={8} collisionPadding={16}>{children}</Popover.Content>
  </div></Popover.Root>;
}
