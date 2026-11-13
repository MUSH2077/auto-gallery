'use client';
import '@/features/gallery-ui/app.css';
import type {ReactNode} from 'react';
import {galleryTypography} from '@/features/gallery-ui/typography';
import {GalleryCopyProvider, galleryCopy} from '@/features/gallery-ui/copy';
import {useI18n} from '@/lib/i18n';
export default function GalleryRoot({children}:{children:ReactNode}) {
  const {lang}=useI18n();
  return <GalleryCopyProvider lang={lang}><div className="gallery-ui gallery-app" data-cjk="mixed" style={galleryTypography}>
    <a href="#main-content" className="skip-link">{galleryCopy(lang,"skipToContent")}</a>{children}
  </div></GalleryCopyProvider>;
}