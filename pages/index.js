import Head from 'next/head'
import Image from 'next/image'
import { Inter } from '@next/font/google'
import styles from '../styles/Home.module.css'
import { getProductsInCollection } from "../lib/shopify"

const inter = Inter({ subsets: ['latin'] })

export default function Home() {
  return (
    <h1 className="text-3xl font-bold">
      Shopify Next.js Project
    </h1>
  )
}

export async function getStaticProps() {
  const products = await getProductsInCollection()

  return {
    props: { }, // will be passed to the page component as props
  }
}