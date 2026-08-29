import { mount } from 'svelte';
import './companion.css';
import Companion from './Companion.svelte';

export default mount(Companion, { target: document.getElementById('app')! });
