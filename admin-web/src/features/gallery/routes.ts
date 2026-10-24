import {adminRoutes} from '@/lib/adminRoutes';
export interface GalleryRoutes {works:string;jobs:string;login:string;}
export const defaultGalleryRoutes:GalleryRoutes={works:adminRoutes.works,jobs:adminRoutes.jobs,login:'/admin/login'};
